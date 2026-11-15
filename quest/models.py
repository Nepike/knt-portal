from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

# Подсказки, которые страница отдаёт сама (журнал, порча, консоль браузера, 404, окно
# отказа), и число фрагментов ключа — в кэше (quest/templatetags/quest_extras.py). Правка
# подсказки или фрагмента в админке его сбрасывает.
CLUES_CACHE = "quest:clues"


def plain(command):
    """Команда консоли в том виде, в каком её ищут: без регистра и лишних пробелов."""
    return " ".join(command.lower().split())


def bare(text):
    """Фрагмент ключа для сверки: без регистра и без пробелов вовсе."""
    return "".join(text.lower().split())


class Clue(models.Model):
    """Подсказка квеста глитч-скина: где её прячем и что в ней написано.

    Тексты живут только здесь, в базе: репозиторий открытый, и подсказка, положенная в код,
    была бы видна в нём раньше, чем на сайте. В коде — одни места, куда они встают.
    """

    class Place(models.TextChoices):
        COMMAND = "command", "ответ на команду в консоли отсчёта"
        LOG = "log", "строка журнала взлома"
        CARD = "card", "проступает в битом тексте (заголовки, где хватает длины)"
        DEVTOOLS = "devtools", "строка в консоли браузера"
        NOTFOUND = "notfound", "строка на странице 404"
        DENIED = "denied", "пятое окно «ОТКАЗАНО» вместо «…»"

    place = models.CharField("где", max_length=16, choices=Place.choices)
    command = models.CharField(
        "команда", max_length=60, blank=True,
        help_text="Только для консоли: что набрать, без учёта регистра и лишних пробелов. "
                  "Свои ответы есть у help, whoami, ls, cat takeover.log, date, sudo, found, "
                  "ls key, cat key/…  — такую команду сайт перехватит раньше подсказки.",
    )
    text = models.CharField("текст", max_length=200)
    laugh = models.BooleanField("со смехом", default=False, help_text="Только для консоли: экран дёрнется, как на sudo.")
    active = models.BooleanField("показывать", default=True)

    class Meta:
        verbose_name = "подсказка"
        verbose_name_plural = "подсказки"
        ordering = ["place", "id"]
        constraints = [
            models.UniqueConstraint(fields=["command"], condition=Q(place="command"), name="quest_clue_one_answer_per_command"),
        ]

    def __str__(self):
        return f"{self.get_place_display()}: {self.command or self.text}"

    def clean(self):
        self.command = plain(self.command)
        if self.place == self.Place.COMMAND and not self.command:
            raise ValidationError({"command": "Для консоли нужна команда."})
        if self.place != self.Place.COMMAND:
            self.command = ""

    def save(self, *args, **kwargs):
        self.command = plain(self.command) if self.place == self.Place.COMMAND else ""
        super().save(*args, **kwargs)
        cache.delete(CLUES_CACHE)

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        cache.delete(CLUES_CACHE)
        return result


class Fragment(models.Model):
    """Часть ключа. Ключ — все части по порядку номеров; что где спрятано — в подсказках.

    Находку человек сдаёт в консоли отсчёта командой found, и та запоминается за ним (Find):
    отсюда его «ключ повреждён: 7f3a ░░░░ …» на экране отсчёта и строка в журнале.
    """

    number = models.PositiveSmallIntegerField("номер", unique=True)
    value = models.CharField(
        "значение", max_length=20,
        help_text="Сверяется без регистра и пробелов. Не короче 4 знаков — короткое подберут перебором, "
                  "и не длиннее 6: весь ключ вводят одной строкой консоли, а в ней 60 знаков.",
    )
    note = models.CharField("где спрятан", max_length=200, blank=True, help_text="Для себя: на сайте не показывается.")

    class Meta:
        verbose_name = "фрагмент ключа"
        verbose_name_plural = "фрагменты ключа"
        ordering = ["number"]

    def __str__(self):
        return f"{self.number}.frag"

    def clean(self):
        # Ключ сверяют без регистра, пробелов и разделителей (quest.keys.squash): со знаком
        # внутри значения он не сошёлся бы ни у кого, а из двух одинаковых значений found
        # всегда находил бы первое — второй фрагмент не сдать.
        value = bare(self.value)
        if not (4 <= len(value) <= 6 and value.isalnum()):
            raise ValidationError({"value": "От 4 до 6 букв или цифр, без пробелов и знаков."})
        if value in {bare(other) for other in Fragment.objects.exclude(pk=self.pk).values_list("value", flat=True)}:
            raise ValidationError({"value": "Такое значение уже есть у другого фрагмента."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        cache.delete(CLUES_CACHE)

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        cache.delete(CLUES_CACHE)
        return result


class Broadcast(models.Model):
    """Передача взломщиков — запись лектория, которую экран отсчёта показывает 1 декабря.

    Видео грузится и печётся как обычная лекция, здесь — только ссылка на неё. До красного
    дня адрес записи на сайт не попадает нигде. Сам курс держат отклонённым: такой видят
    только автор и модераторы, а не весь лекторий. Запись одна на сайт.
    """

    lecture = models.ForeignKey(
        "lectorium.Lecture", verbose_name="запись", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text="Лекция с видео передачи. Курс — отдельный и со статусом «отклонено», иначе его найдут в лектории.",
    )
    frame = models.ForeignKey(
        "cosmetics.CosmeticItem", verbose_name="рамка за ключ", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", help_text="Вещь, которую получает каждый, кто ввёл ключ. Не в продаже — галочку «продаётся» снять.",
    )

    class Meta:
        verbose_name = "передача"
        verbose_name_plural = "передача"

    def __str__(self):
        return "Передача взломщиков"

    @classmethod
    def current(cls):
        return cls.objects.select_related("lecture", "frame").first()


class Unlock(models.Model):
    """Человек ввёл ключ целиком — раз на каждого. За это — награда (economy.rewards) и рамка."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.CASCADE, related_name="+")
    at = models.DateTimeField("когда", auto_now_add=True)

    class Meta:
        verbose_name = "вскрытый ключ"
        verbose_name_plural = "вскрытые ключи"
        ordering = ["at"]

    def __str__(self):
        return str(self.user)


class Find(models.Model):
    """Человек сдал найденный фрагмент — раз на каждый."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.CASCADE, related_name="+")
    fragment = models.ForeignKey(Fragment, verbose_name="фрагмент", on_delete=models.CASCADE, related_name="finds")
    at = models.DateTimeField("когда", auto_now_add=True)

    class Meta:
        verbose_name = "находка"
        verbose_name_plural = "находки"
        ordering = ["-at"]
        constraints = [models.UniqueConstraint(fields=["user", "fragment"], name="quest_find_once")]

    def __str__(self):
        return f"{self.user} — {self.fragment}"


class Tally(models.Model):
    """Счётчики события — для праздничной страницы. Пока один: сколько команд набрали в консоли
    отсчёта (quest.views.ask). Строкой в базе, а не в кэше: там счёт пропал бы с первым
    сбросом Redis, а копится он двенадцать дней."""

    COMMANDS = "commands"

    name = models.CharField("что считаем", max_length=30, unique=True)
    count = models.PositiveBigIntegerField("сколько", default=0)

    class Meta:
        verbose_name = "счётчик"
        verbose_name_plural = "счётчики"

    def __str__(self):
        return self.name

    @classmethod
    def bump(cls, name):
        """Прибавить единицу. Обычно — один UPDATE; строку заводит первый же вызов."""
        if not cls.objects.filter(name=name).update(count=F("count") + 1):
            cls.objects.get_or_create(name=name)
            cls.objects.filter(name=name).update(count=F("count") + 1)

    @classmethod
    def of(cls, name):
        return cls.objects.filter(name=name).values_list("count", flat=True).first() or 0
