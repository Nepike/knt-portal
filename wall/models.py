from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.utils import timezone

from .palette import EMPTY
from .rules import MAX_CHARGES


class Board(models.Model):
    """Полотно. Текущее ровно одно, прошлые остаются архивом семестра.

    Размер небольшой намеренно: активных у нас десятки, а не миллионы, и большое
    полотно выглядело бы заброшенным. Растёт вправо и вниз, чтобы уже поставленные
    пиксели не поехали по координатам.
    """

    title = models.CharField("название", max_length=100)
    # Потолок в байт не случаен: журнал для таймлапса пишется по три байта на событие
    # (x, y, цвет), и доска шире 256 клеток в этот формат уже не влезет.
    width = models.PositiveSmallIntegerField("ширина", default=128, validators=[MaxValueValidator(255)])
    height = models.PositiveSmallIntegerField("высота", default=72, validators=[MaxValueValidator(255)])
    is_active = models.BooleanField("текущая", default=True)
    created = models.DateTimeField("открыта", default=timezone.now)
    closed = models.DateTimeField("закрыта", null=True, blank=True)

    class Meta:
        verbose_name = "доска"
        verbose_name_plural = "доски"
        ordering = ["-created"]
        permissions = [("moderate_wall", "Может править Стену")]
        constraints = [
            # Иначе непонятно, какую из двух показывать и куда писать пиксели.
            models.UniqueConstraint(
                fields=["is_active"], condition=models.Q(is_active=True), name="single_active_board",
            ),
        ]

    def __str__(self):
        return self.title

    @classmethod
    def current(cls):
        return cls.objects.filter(is_active=True).first()

    def holds(self, x, y):
        return 0 <= x < self.width and 0 <= y < self.height


class Pixel(models.Model):
    """Нынешнее состояние клетки.

    Стёртая клетка не удаляется, а остаётся строкой с цветом EMPTY: так видно, кто её
    стёр, и путь записи получается один и тот же на закраску и на стирание.
    """

    board = models.ForeignKey(Board, verbose_name="доска", on_delete=models.CASCADE, related_name="pixels")
    x = models.PositiveSmallIntegerField()
    y = models.PositiveSmallIntegerField()
    color = models.PositiveSmallIntegerField("цвет", default=EMPTY)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="pixels",
    )
    placed = models.DateTimeField("когда", default=timezone.now)

    class Meta:
        verbose_name = "пиксель"
        verbose_name_plural = "пиксели"
        constraints = [models.UniqueConstraint(fields=["board", "x", "y"], name="unique_pixel")]

    def __str__(self):
        return f"({self.x}, {self.y})"


class Placement(models.Model):
    """Журнал доски, только на вставку.

    Из него живут три вещи сразу: история клетки («кто это поставил»), перемотка всей
    доски и откат области модератором. Стирания пишутся сюда же — иначе на пустой
    клетке не узнать, кто её опустошил.
    """

    board = models.ForeignKey(Board, verbose_name="доска", on_delete=models.CASCADE, related_name="placements")
    x = models.PositiveSmallIntegerField()
    y = models.PositiveSmallIntegerField()
    color = models.PositiveSmallIntegerField("цвет")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="placements",
    )
    created = models.DateTimeField("когда", default=timezone.now)

    class Meta:
        verbose_name = "событие доски"
        verbose_name_plural = "события доски"
        # id, а не created: у одновременных событий таймстемпы совпадают, а порядок
        # важен — он же номер версии, которым клиент догоняет пропущенное.
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["board", "x", "y", "-id"]),
            models.Index(fields=["board", "id"]),
        ]

    def __str__(self):
        return f"({self.x}, {self.y}) от {self.user}"


class ProtectedArea(models.Model):
    """Замороженный участок: готовый рисунок, который не дают перекрашивать.

    Прямоугольник, а не перечень клеток: модератор защищает работу целиком, а держать
    и проверять тысячи отдельных клеток на каждый чужой мазок вышло бы куда дороже.
    Модератора рамка не держит — иначе он не смог бы убрать то, что сам и закрыл.
    """

    board = models.ForeignKey(Board, verbose_name="доска", on_delete=models.CASCADE, related_name="areas")
    x1 = models.PositiveSmallIntegerField()
    y1 = models.PositiveSmallIntegerField()
    x2 = models.PositiveSmallIntegerField()
    y2 = models.PositiveSmallIntegerField()
    note = models.CharField("что это", max_length=100, blank=True)
    by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="закрыл", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    created = models.DateTimeField("когда", default=timezone.now)

    class Meta:
        verbose_name = "закрытый участок"
        verbose_name_plural = "закрытые участки"
        ordering = ["-id"]

    def __str__(self):
        return self.note or f"({self.x1}, {self.y1}) — ({self.x2}, {self.y2})"


class WallProfile(models.Model):
    """Запас пикселей человека и его счётчик закрашенного.

    Заряды не начисляются по расписанию: храним, сколько их было в момент charged_at,
    а сколько есть сейчас — считаем при обращении. Иначе понадобилась бы задача,
    которая каждые три минуты обходит всех пользователей разом.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="владелец", on_delete=models.CASCADE, related_name="wall",
    )
    charges = models.PositiveSmallIntegerField("заряды", default=MAX_CHARGES)
    charged_at = models.DateTimeField("отсчёт зарядов", default=timezone.now)
    # Считаем ЗДЕСЬ, а не по журналу доски: в него пишут и заливки модератора, и консоль,
    # а награда полагается только за мазок, оплаченный зарядом. Растёт в _take_charge.
    painted = models.PositiveIntegerField("закрашено клеток", default=0)
    banned_until = models.DateTimeField("доска закрыта до", null=True, blank=True)

    class Meta:
        verbose_name = "участник Стены"
        verbose_name_plural = "участники Стены"

    def __str__(self):
        return str(self.user)
