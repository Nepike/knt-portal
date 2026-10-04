"""Очередь заданий на выпечку.

Пекарня — обычный клиент, а не сервер: она сама приходит и спрашивает, есть ли работа.
Отсюда и очередь на стороне сайта. Задание выдаётся ровно одной машине, а если та
пропала, оно через час возвращается в очередь — иначе одна упавшая пекарня заморозила
бы лекцию навсегда.

Договор целиком — в `docs/media-pipeline.md`.
"""

from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

# Столько ждём вестей от взявшего задание. Двухчасовая лекция печётся минут двадцать,
# час — с запасом на медленную машину и на скачивание сырья.
CLAIM_TIMEOUT = 3600
# Столько раз задание выдаётся, прежде чем сайт признаёт: не испечётся. Возвраты
# в очередь честны поодиночке — обрыв связи, остановка на ночь, — но задание, которое
# отдают снова и снова, сломано само, и без предела оно крутится вечно: 30.09.2026
# одно такое набрало 32767 выдач, переполнило счётчик и заперло собой всю очередь.
MAX_TRIES = 20


class MediaJob(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", "ждёт"
        BAKING = "baking", "печётся"
        DONE = "done", "готово"
        FAILED = "failed", "не вышло"

    recipe = models.CharField("рецепт", max_length=40)
    source = models.CharField("сырьё (ключ)", max_length=200)
    lecture = models.OneToOneField(
        "lectorium.Lecture", verbose_name="лекция", on_delete=models.CASCADE,
        null=True, blank=True, related_name="job",
    )

    status = models.CharField(
        "состояние", max_length=10, choices=Status.choices, default=Status.WAITING, db_index=True,
    )
    # Куда пекарня сложила готовое. Заполняется на `plan`, а к лекции привязывается
    # только на `commit`: между этими шагами набор ещё неполон.
    prefix = models.CharField("папка готового", max_length=200, blank=True)
    manifest = models.JSONField("описание готового", default=dict, blank=True)

    claimed_by = models.CharField("кто взял", max_length=100, blank=True)
    claimed_at = models.DateTimeField("когда взяли", null=True, blank=True)
    # Номер попытки. Живёт в токене задания (intake.views._job) и потому только растёт:
    # сброс воскресил бы старые токены. По той же причине не smallint — его 32767
    # однажды кончились.
    attempts = models.PositiveIntegerField("попыток", default=0)
    # Выдач с тех пор, как задание встало в очередь. В отличие от `attempts`, это счётчик
    # для предела (MAX_TRIES), и его сбрасывает человек, возвращая задание из админки.
    tries = models.PositiveSmallIntegerField("выдач подряд", default=0)
    note = models.CharField("что пошло не так", max_length=300, blank=True)

    created = models.DateTimeField("создано", default=timezone.now)
    updated = models.DateTimeField("изменено", auto_now=True)

    class Meta:
        verbose_name = "задание"
        verbose_name_plural = "очередь выпечки"
        ordering = ["-created"]
        constraints = [
            # Сырьё снимается, как только задание закрыто: второму заданию на тот же ключ
            # печь было бы не из чего.
            models.UniqueConstraint(fields=["source"], name="intake_job_source_once"),
        ]

    def __str__(self):
        return f"#{self.pk} {self.recipe} ({self.get_status_display()})"

    @property
    def lost(self):
        """Взято, но вестей нет слишком долго — машина упала или её выключили."""
        return (
            self.status == self.Status.BAKING
            and self.claimed_at
            and (timezone.now() - self.claimed_at).total_seconds() > CLAIM_TIMEOUT
        )


def sweep(prefix):
    """Снять папку готового — но только если на неё не смотрит ни одна лекция.

    Проверка не формальность. После `commit` папка задания И ЕСТЬ набор живой лекции,
    а задание возвращают в очередь руками из админки, чтобы перепечь; без этой проверки
    «прошлая папка» задания оказалась бы тем самым набором, который сейчас смотрят,
    и опубликованная лекция осталась бы указывать в пустоту. Прошлый набор снимает
    `commit` — тогда, когда новый уже встал на его место.
    """
    from lectorium.models import Lecture
    from lectorium.tasks import drop_lecture_files

    if prefix and not Lecture.objects.filter(prefix=prefix).exists():
        transaction.on_commit(lambda: drop_lecture_files.delay(prefix))


@receiver(post_delete, sender=MediaJob, dispatch_uid="intake.leftovers")
def _drop_leftovers(sender, instance, **kwargs):
    """Убрать за удалённым заданием.

    Запись удалили, пока она пеклась, — задание уезжает каскадом, и вместе с ним
    пропадает единственная память о двух вещах: о сырье (десятки гигабайт) и о папке,
    куда пекарня уже успела налить кусков. `post_delete` самой лекции про них не знает:
    до `commit` её `prefix` пуст. Без этого обеих потом не найти ничем.

    У закрытого задания сырьё снял `commit`, а папка принадлежит лекции — обе ветки
    об этом помнят.
    """
    from .tasks import drop_source

    sweep(instance.prefix)
    if instance.status != MediaJob.Status.DONE and instance.source:
        transaction.on_commit(lambda: drop_source.delay(instance.source))


def take(worker):
    """Выдать одно задание этой машине. None — работы нет.

    `select_for_update(skip_locked=True)`: две пекарни, пришедшие разом, не должны
    получить одну лекцию. Занятую строку вторая просто пропускает и берёт следующую,
    а не ждёт освобождения — ждать ей нечего, работы хватает.

    Заодно подбираем брошенные: задание, взятое час назад и молчащее, вернулось
    в очередь. Иначе упавшая машина заморозила бы лекцию навсегда.

    А выданное MAX_TRIES раз и так и не доделанное больше не выдаём — закрываем отказом,
    как если бы пекарня сказала `fail`. Очередь идёт от старых к новым, и такое задание
    стояло бы в её голове вечно: каждая пекарня первым делом получала бы его.
    """
    stale = timezone.now() - timezone.timedelta(seconds=CLAIM_TIMEOUT)
    with transaction.atomic():
        free = (
            MediaJob.objects.select_for_update(skip_locked=True)
            .filter(
                models.Q(status=MediaJob.Status.WAITING)
                | models.Q(status=MediaJob.Status.BAKING, claimed_at__lt=stale)
            )
            .order_by("created")
        )
        for worn in free.filter(tries__gte=MAX_TRIES):
            _give_up(worn)
        job = free.filter(tries__lt=MAX_TRIES).first()
        if job is None:
            return None
        job.status = MediaJob.Status.BAKING
        job.claimed_by = worker[:100]
        job.claimed_at = timezone.now()
        job.attempts += 1
        job.tries += 1
        job.save(update_fields=["status", "claimed_by", "claimed_at", "attempts", "tries", "updated"])
    return job


def _give_up(job):
    """Закрыть задание, которое раз за разом берут и не доделывают. Причина уходит
    человеку — так же, как отказ пекарни (intake.views.fail), и так же снимается папка."""
    stale, job.prefix = job.prefix, ""
    job.status = MediaJob.Status.FAILED
    job.note = f"пекарня бралась за запись {job.tries} раз и ни разу не довела её до конца"
    job.save(update_fields=["status", "note", "prefix", "updated"])
    sweep(stale)
