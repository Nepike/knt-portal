from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.utils import timezone

from .palette import EMPTY

# «Навсегда» для бана: дата, до которой никто не доживёт, — проверка остаётся одной.
FOREVER = datetime(9999, 1, 1, tzinfo=dt_timezone.utc)


class Board(models.Model):
    """Полотно. Текущее одно, прошлые — архив семестра. Размер небольшой намеренно; растёт
    вправо и вниз, чтобы поставленные пиксели не поехали."""

    title = models.CharField("название", max_length=100)
    # Не больше 255: журнал таймлапса пишет по три байта на событие (x, y, цвет).
    width = models.PositiveSmallIntegerField("ширина", default=128, validators=[MaxValueValidator(255)])
    height = models.PositiveSmallIntegerField("высота", default=72, validators=[MaxValueValidator(255)])
    is_active = models.BooleanField("текущая", default=True)
    created = models.DateTimeField("открыта", default=timezone.now)
    closed = models.DateTimeField("закрыта", null=True, blank=True)

    class Meta:
        verbose_name = "доска"
        verbose_name_plural = "доски"
        ordering = ["-created"]
        permissions = [
            ("moderate_wall", "Может править Стену"),
            ("stamp_wall", "Может наносить картинки на Стену"),
        ]
        constraints = [
            # Активная доска — одна: иначе непонятно, какую показывать.
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
    """Нынешнее состояние клетки. Стёртая остаётся строкой с EMPTY: видно, кто стёр."""

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
    """Журнал доски, только на вставку: история клетки, перемотка и откат области. Стирания —
    сюда же, иначе на пустой клетке не узнать, кто её опустошил."""

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
        # id, а не created: таймстемпы совпадают, а порядок — номер версии для догона.
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["board", "x", "y", "-id"]),
            models.Index(fields=["board", "id"]),
        ]

    def __str__(self):
        return f"({self.x}, {self.y}) от {self.user}"


class ProtectedArea(models.Model):
    """Замороженный участок — прямоугольником, а не перечнем клеток (проверка на каждый мазок).
    Модератора рамка не держит."""

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
    """Счётчик закрашенного в счёт награды и бан на доске."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="владелец", on_delete=models.CASCADE, related_name="wall",
    )
    # Не журнал: в награду идут только мазки кистью и не больше rules.COUNTED_PER_DAY в сутки.
    painted = models.PositiveIntegerField("закрашено в счёт награды", default=0)
    counted_on = models.DateField("день счёта", null=True, blank=True)
    counted_today = models.PositiveSmallIntegerField("в счёт за день", default=0)
    banned_until = models.DateTimeField("доска закрыта до", null=True, blank=True)
    ban_reason = models.CharField("за что", max_length=200, blank=True)
    banned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кто закрыл", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "участник Стены"
        verbose_name_plural = "участники Стены"

    def __str__(self):
        return str(self.user)

    @property
    def banned(self):
        return bool(self.banned_until and self.banned_until > timezone.now())

    @property
    def forever(self):
        return bool(self.banned_until and self.banned_until.year >= FOREVER.year)
