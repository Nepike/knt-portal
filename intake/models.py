"""Очередь заданий на выпечку. Пекарня сама приходит за работой; задание выдаётся одной
машине и через час без вестей возвращается в очередь. Договор — `docs/media-pipeline.md`.
"""

from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from core.failures import record
from core.models import Failure

# Столько ждём вестей от взявшего задание: двухчасовая лекция печётся минут двадцать.
CLAIM_TIMEOUT = 3600
# Столько выдач, прежде чем задание признаётся неиспекаемым: без предела сломанное задание
# крутится вечно и запирает собой очередь.
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
    # Куда пекарня сложила готовое: заполняется на `plan`, к лекции привязывается на `commit`.
    prefix = models.CharField("папка готового", max_length=200, blank=True)
    manifest = models.JSONField("описание готового", default=dict, blank=True)

    claimed_by = models.CharField("кто взял", max_length=100, blank=True)
    claimed_at = models.DateTimeField("когда взяли", null=True, blank=True)
    # Номер попытки. Живёт в токене задания (intake.views._job) и только растёт: сброс
    # воскресил бы старые токены. Не smallint — однажды 32767 кончились.
    attempts = models.PositiveIntegerField("попыток", default=0)
    # Выдачи подряд — для предела MAX_TRIES; сбрасывает человек, возвращая задание из админки.
    tries = models.PositiveSmallIntegerField("выдач подряд", default=0)
    note = models.CharField("что пошло не так", max_length=300, blank=True)

    created = models.DateTimeField("создано", default=timezone.now)
    updated = models.DateTimeField("изменено", auto_now=True)

    class Meta:
        verbose_name = "задание"
        verbose_name_plural = "очередь выпечки"
        ordering = ["-created"]
        constraints = [
            # Сырьё снимается, как только задание закрыто: второму заданию печь было бы не из чего.
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


class Bakery(models.Model):
    """Машина-пекарня глазами сайта: когда была на связи и что с ней не так (intake.health).
    Неполадку приносит сама (`tools/bake.checkup`), молчание замечает сайт. Строка на машину."""

    name = models.CharField("машина", max_length=100, unique=True)
    seen_at = models.DateTimeField("на связи", default=timezone.now)
    problem = models.CharField("неполадка", max_length=300, blank=True)
    # О чём уже сказано в чат: пишем только смену.
    alert = models.CharField("сказано в чат", max_length=300, blank=True)

    class Meta:
        verbose_name = "пекарня"
        verbose_name_plural = "пекарни"
        ordering = ["-seen_at"]

    def __str__(self):
        return self.name


def sweep(prefix):
    """Снять папку готового, только если на неё не смотрит ни одна лекция: после `commit`
    папка задания — набор живой лекции, а задание могут вернуть на перепечку. Прошлый набор
    снимает `commit`, когда новый встал на место."""
    from lectorium.models import Lecture
    from lectorium.tasks import drop_lecture_files

    if prefix and not Lecture.objects.filter(prefix=prefix).exists():
        transaction.on_commit(lambda: drop_lecture_files.delay(prefix))


@receiver(post_delete, sender=MediaJob, dispatch_uid="intake.leftovers")
def _drop_leftovers(sender, instance, **kwargs):
    """Убрать за удалённым заданием: сырьё и папку с кусками. `post_delete` лекции о них не
    знает — до `commit` её `prefix` пуст. У закрытого задания сырьё снял `commit`."""
    from .tasks import drop_source

    sweep(instance.prefix)
    if instance.status != MediaJob.Status.DONE and instance.source:
        transaction.on_commit(lambda: drop_source.delay(instance.source))


def take(worker):
    """Выдать одно задание этой машине; None — работы нет.

    `select_for_update(skip_locked=True)`: две пекарни не получат одну лекцию. Заодно
    брошенное (взято час назад, молчит) возвращается в очередь, а выданное MAX_TRIES раз
    закрывается отказом — иначе стояло бы в голове очереди вечно.
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
    """Закрыть задание, которое раз за разом берут и не доделывают: причина — человеку, как
    при отказе пекарни (intake.views.fail), папка снимается."""
    stale, job.prefix = job.prefix, ""
    job.status = MediaJob.Status.FAILED
    job.note = f"пекарня бралась за запись {job.tries} раз и ни разу не довела её до конца"
    job.save(update_fields=["status", "note", "prefix", "updated"])
    sweep(stale)
    failed_bake(job)


def failed_bake(job):
    """В журнал сбоев: запись, которую пекарня не испекла."""
    from notifications import services as notifications  # внутри: уведомления знают про лекции, а те — про очередь

    what = f"«{job.lecture.title}»" if job.lecture else job.source
    record(Failure.Source.BAKERY, f"{what}: {job.note}")
    notifications.bake_failed(job)
