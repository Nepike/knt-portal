"""Печать через сайт: принтеры, задания, лимиты, показания счётчиков.

Документ сайт не открывает: страницы, заливку и превью приносит агент на М4
(`tools/print_agent.py`), он же отдаёт задание в CUPS. Здесь — учёт. Договор — `docs/printing.md`.
"""

from datetime import timedelta
from pathlib import PurePosixPath
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone

from .pricing import DARK, Costs

# Агент шлёт показания раз в минуту. Пять минут тишины — связи с принтером нет,
# что бы ни говорило последнее показание.
OFFLINE_AFTER = timedelta(minutes=5)
LOW_TONER = 10  # процентов


class Printer(models.Model):
    class State(models.TextChoices):
        READY = "ready", "готов"
        BUSY = "busy", "печатает"
        NO_PAPER = "no_paper", "нет бумаги"
        JAM = "jam", "замятие"
        OPEN = "open", "открыта крышка"
        NO_TONER = "no_toner", "нет тонера"
        OFFLINE = "offline", "нет связи"
        UNKNOWN = "unknown", "неизвестно"

    name = models.CharField("название", max_length=100)
    place = models.CharField("где стоит", max_length=200, blank=True, help_text="Показывается студенту")
    queue = models.CharField("очередь CUPS", max_length=100, unique=True, help_text="Имя очереди на агенте, например m4-kyocera")
    address = models.GenericIPAddressField("адрес", protocol="IPv4", help_text="По нему агент спрашивает принтер по SNMP")
    active = models.BooleanField("принимает задания", default=True)
    duplex = models.BooleanField("двусторонняя печать", default=True)

    # Себестоимость. Числа по умолчанию — условные из плана, настоящие вписываются здесь.
    paper_sheet = models.PositiveIntegerField("лист бумаги, коп.", default=100)
    toner_price = models.PositiveIntegerField("картридж, коп.", default=150000)
    toner_yield = models.PositiveIntegerField("ресурс картриджа, стр.", default=7200, help_text="При заливке 5%, как пишет производитель")
    drum_price = models.PositiveIntegerField("барабан, коп.", default=300000)
    drum_yield = models.PositiveIntegerField("ресурс барабана, стр.", default=100000)
    toner_factor = models.FloatField("поправка тонера", default=1.0, help_text="Даёт калибровка по счётчику; пока 1")

    # Что последним прислал агент.
    state = models.CharField("состояние", max_length=10, choices=State.choices, default=State.UNKNOWN)
    state_text = models.CharField("текст на панели", max_length=200, blank=True)
    toner_percent = models.PositiveSmallIntegerField("тонер, %", null=True, blank=True)
    page_counter = models.PositiveIntegerField("счётчик оттисков", null=True, blank=True)
    seen_at = models.DateTimeField("показания от", null=True, blank=True)
    # О какой неполадке уже написали в телеграм (printing.alerts): пишем на смене, а не
    # с каждым показанием — иначе «нет бумаги» приходило бы раз в минуту до утра.
    alert = models.CharField("о чём сообщено", max_length=12, blank=True)

    class Meta:
        verbose_name = "принтер"
        verbose_name_plural = "принтеры"
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name

    def costs(self):
        return Costs(
            paper_sheet=self.paper_sheet, toner_price=self.toner_price, toner_yield=self.toner_yield,
            drum_price=self.drum_price, drum_yield=self.drum_yield, toner_factor=self.toner_factor,
        )

    @property
    def online(self):
        """Старое показание — это «нет связи», а не последнее известное «готов»: агент молчит,
        и печатать сейчас некому, сколько бы бумаги ни было в лотке."""
        return bool(self.seen_at) and timezone.now() - self.seen_at <= OFFLINE_AFTER and self.state != self.State.OFFLINE

    @property
    def available(self):
        """Можно ли выбрать для печати. Нет бумаги — можно: задание дождётся её в очереди."""
        return self.active and self.online

    def condition(self):
        """(что сказать студенту, тон): ok, warn или bad."""
        if not self.active:
            return "Не принимает задания", "bad"
        if not self.online:
            return "Нет связи с принтером", "bad"
        if self.state in (self.State.NO_PAPER, self.State.JAM, self.State.OPEN, self.State.NO_TONER):
            return self.get_state_display().capitalize(), "bad"
        if self.toner_percent is not None and self.toner_percent < LOW_TONER:
            return "Мало тонера", "warn"
        if self.state == self.State.BUSY:
            return "Печатает", "ok"
        if self.state == self.State.READY:
            return "Готов", "ok"
        return "Состояние неизвестно", "warn"


class PrintJob(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", "загружено"
        ANALYZING = "analyzing", "готовим превью"
        READY = "ready", "ждёт выбора"
        REJECTED = "rejected", "не принято"
        EXPIRED = "expired", "срок истёк"
        QUEUED = "queued", "в очереди"
        PRINTING = "printing", "печатается"
        DONE = "done", "напечатано"
        FAILED = "failed", "не вышло"
        CANCELED = "canceled", "отменено"
        UNKNOWN = "unknown", "неизвестно"

    # Путь задания (docs/printing.md, «Путь задания»).
    PREPARING = frozenset({Status.UPLOADED, Status.ANALYZING})
    ACTIVE = frozenset({Status.QUEUED, Status.PRINTING})
    FINAL = frozenset({Status.REJECTED, Status.EXPIRED, Status.DONE, Status.FAILED, Status.CANCELED, Status.UNKNOWN})
    # Что идёт в недельный лимит. `unknown` — нет: агент замолчал после CUPS, и лист мог
    # не выйти; сомнение — в пользу студента.
    COUNTED = frozenset({Status.QUEUED, Status.PRINTING, Status.DONE})
    # После чего можно напечатать тот же файл ещё раз, пока он не удалён.
    REPEATABLE = frozenset({Status.DONE, Status.FAILED, Status.CANCELED, Status.UNKNOWN})

    # Он же папка файлов в хранилище: printing/<uuid>/. Повтор берёт файлы предшественника
    # и папку свою не заводит — поэтому папку знают ключи, а не uuid.
    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.CASCADE, related_name="print_jobs")
    printer = models.ForeignKey(Printer, verbose_name="принтер", on_delete=models.PROTECT, related_name="jobs")
    status = models.CharField("состояние", max_length=10, choices=Status.choices, default=Status.UPLOADED, db_index=True)
    name = models.CharField("файл", max_length=200)
    source_key = models.CharField("исходник (ключ)", max_length=200)
    pdf_key = models.CharField("print.pdf (ключ)", max_length=200, blank=True)

    # От агента: что выйдет на бумагу.
    pages = models.PositiveIntegerField("страниц", default=0)
    coverage = models.JSONField("заливка по страницам", default=list, blank=True)
    sideways = models.JSONField("поперёк листа", default=list, blank=True, help_text="Номера альбомных страниц")

    # Выбор человека — снимок на момент «Печатать».
    selection = models.CharField("страницы", max_length=200, blank=True)
    copies = models.PositiveSmallIntegerField("копий", default=1)
    duplex = models.BooleanField("двусторонняя", default=True)
    sides = models.PositiveIntegerField("в лимит, стр.", default=0)
    cost = models.PositiveIntegerField("себестоимость, коп.", default=0)
    costs = models.JSONField("цены в расчёте", default=dict, blank=True)
    ip = models.GenericIPAddressField("адрес", null=True, blank=True)
    outside_m4 = models.BooleanField("не из М4", default=False)
    reason = models.CharField("причина", max_length=300, blank=True)

    created = models.DateTimeField("загружено", default=timezone.now)
    analyzed_at = models.DateTimeField("разобрано", null=True, blank=True)
    submitted_at = models.DateTimeField("отправлено", null=True, blank=True)
    finished_at = models.DateTimeField("закончено", null=True, blank=True)
    files_dropped = models.BooleanField("файлы удалены", default=False)

    # Для агента — как у intake.MediaJob.
    attempts = models.PositiveIntegerField("попыток", default=0)
    tries = models.PositiveSmallIntegerField("выдач подряд", default=0)
    claimed_at = models.DateTimeField("когда взяли", null=True, blank=True)
    heard_at = models.DateTimeField("последняя весть", null=True, blank=True)
    cups_job = models.PositiveIntegerField("задание CUPS", null=True, blank=True)

    class Meta:
        verbose_name = "задание печати"
        verbose_name_plural = "задания печати"
        ordering = ["-created", "-pk"]
        indexes = [models.Index(fields=["user", "submitted_at"], name="printing_job_week")]
        permissions = [
            ("use", "печатать через сайт"),
            ("unlimited", "печатать без недельного лимита"),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    @property
    def folder(self):
        return f"{PurePosixPath(self.source_key).parent}/"

    @property
    def can_print(self):
        return self.status == self.Status.READY and not self.files_dropped

    @property
    def can_repeat(self):
        return self.status in self.REPEATABLE and not self.files_dropped and bool(self.pdf_key)

    @property
    def waiting(self):
        """Ещё изменится само — страница спрашивает о нём."""
        return self.status in self.PREPARING or self.status in self.ACTIVE

    def pictures(self):
        """Страницы для сетки: номер, ключ превью, заливка, тёмная ли, поперёк ли."""
        sideways = set(self.sideways)
        for number in range(1, self.pages + 1):
            share = self.coverage[number - 1] if number <= len(self.coverage) else 0.0
            yield {
                "number": number, "key": f"{self.folder}pages/{number:04d}.jpg",
                "share": share, "dark": share > DARK, "sideways": number in sideways,
            }


class PrintAccount(models.Model):
    """Отдельная строка, как Wallet: под её блокировкой ставится задание в очередь
    (printing.services.submit), и в ней же личный лимит, если он не общий."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.CASCADE, related_name="print_account")
    weekly_limit = models.PositiveIntegerField(
        "свой лимит, стр. в неделю", null=True, blank=True,
        help_text="Пусто — общий лимит сайта. Без лимита — право «печатать без недельного лимита»",
    )

    class Meta:
        verbose_name = "лимит печати"
        verbose_name_plural = "лимиты печати"

    def __str__(self):
        return str(self.user)


class PrinterReading(models.Model):
    """Показания по SNMP — для калибровки тонера и сверки «счётчик минус задания сайта»."""

    printer = models.ForeignKey(Printer, verbose_name="принтер", on_delete=models.CASCADE, related_name="readings")
    at = models.DateTimeField("когда", default=timezone.now, db_index=True)
    page_counter = models.PositiveIntegerField("счётчик оттисков", null=True, blank=True)
    toner_percent = models.PositiveSmallIntegerField("тонер, %", null=True, blank=True)
    state = models.CharField("состояние", max_length=10, choices=Printer.State.choices, blank=True)

    class Meta:
        verbose_name = "показание принтера"
        verbose_name_plural = "показания принтеров"
        ordering = ["-at"]

    def __str__(self):
        return f"{self.printer} {self.at:%d.%m %H:%M}"
