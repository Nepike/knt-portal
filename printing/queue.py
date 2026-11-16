"""Очередь агента печати — как у пекарни (`intake.models.take`): агент приходит сам, срок
тишины — от последней вести. Главное отличие: отданное в CUPS (`sent`) назад в очередь не
встаёт никогда — при неясном исходе решает человек (`unknown`).
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.failures import record
from core.models import Failure

from .models import PrintJob

Status = PrintJob.Status

# Разбор: агент шлёт весточки по ходу.
ANALYZE_SILENCE = timedelta(minutes=5)
# Печать до `sent` — дело секунд.
PRINT_SILENCE = timedelta(minutes=2)
# После `sent` агент отчитывается раз в полминуты; пока говорит, ждём сколько угодно.
SENT_SILENCE = timedelta(minutes=30)
# Предел выдач: без него задание, на котором агент падает, стояло бы в голове очереди вечно.
ANALYZE_TRIES = 5
PRINT_TRIES = 10


def _hand_out(job, now):
    job.attempts += 1
    job.tries += 1
    job.claimed_at = job.heard_at = now
    return job


def failed_print(job):
    """В журнал сбоев: печать, не дошедшая до бумаги. Отказ разбора — беда файла, его не пишем."""
    record(Failure.Source.PRINTING, f"«{job.name}», {job.user.full_name}: {job.reason}")


def take_analysis():
    """Следующий файл на разбор или None. Брошенный разбор выдаётся снова — с новой попыткой."""
    now = timezone.now()
    with transaction.atomic():
        free = (
            PrintJob.objects.select_for_update(skip_locked=True)
            .filter(Q(status=Status.UPLOADED) | Q(status=Status.ANALYZING, heard_at__lt=now - ANALYZE_SILENCE))
            .order_by("created", "pk")
        )
        for worn in free.filter(tries__gte=ANALYZE_TRIES):
            worn.status = Status.REJECTED
            worn.reason = "Не удалось подготовить файл к печати. Сохрани его в PDF и загрузи снова."
            worn.save(update_fields=["status", "reason"])
        job = free.filter(tries__lt=ANALYZE_TRIES).first()
        if job is None:
            return None
        _hand_out(job, now).status = Status.ANALYZING
        job.save(update_fields=["status", "attempts", "tries", "claimed_at", "heard_at"])
    return job


def take_print():
    """Следующее задание на печать или None — по очереди нажатий и по одному на принтер:
    иначе не понять, чьи листы в лотке."""
    now = timezone.now()
    stale = now - PRINT_SILENCE
    with transaction.atomic():
        busy = PrintJob.objects.filter(status=Status.PRINTING).filter(
            Q(cups_job__isnull=False) | Q(heard_at__gte=stale)
        ).values("printer")
        # of=("self",): иначе Postgres запер бы и строки принтеров из условия.
        free = (
            PrintJob.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(
                Q(status=Status.QUEUED)
                | Q(status=Status.PRINTING, cups_job__isnull=True, heard_at__lt=stale)
            )
            .filter(printer__active=True)
            .exclude(printer__in=busy)
            .order_by("submitted_at", "pk")
        )
        for worn in free.filter(tries__gte=PRINT_TRIES):
            worn.status = Status.FAILED
            worn.reason = "агент не смог отдать задание принтеру"
            worn.finished_at = now
            worn.save(update_fields=["status", "reason", "finished_at"])
            failed_print(worn)
        job = free.filter(tries__lt=PRINT_TRIES).first()
        if job is None:
            return None
        _hand_out(job, now).status = Status.PRINTING
        job.save(update_fields=["status", "attempts", "tries", "claimed_at", "heard_at"])
    return job


def settle():
    """Отданное в CUPS и замолчавшее — `unknown`: в лимит не идёт и само не печатается.
    Зовётся с каждым запросом агента, а без агента — ночной задачей."""
    now = timezone.now()
    return PrintJob.objects.filter(
        status=Status.PRINTING, cups_job__isnull=False, heard_at__lt=now - SENT_SILENCE,
    ).update(
        status=Status.UNKNOWN, finished_at=now,
        reason="агент перестал отвечать, когда задание уже ушло на принтер",
    )
