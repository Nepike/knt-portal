"""Недельный лимит и постановка в очередь. Лимит считается по заданиям недели
(`PrintJob.COUNTED`): отмена, отказ и `unknown` возвращают страницы сами.
"""

from datetime import datetime, time, timedelta
from functools import partial

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from attachments.storage import drop_prefix

from . import pricing
from .models import Printer, PrintAccount, PrintJob

UNLIMITED = "printing.unlimited"
MAX_COPIES = 20
BANNED = "Печать для тебя закрыта. Если это ошибка — напиши в поддержку."


class Refused(Exception):
    """Отказ, который увидит человек."""


def week_start(now=None):
    """Понедельник 00:00 по Москве (TIME_ZONE проекта) той недели, где лежит now."""
    local = timezone.localtime(now or timezone.now())
    monday = local.date() - timedelta(days=local.weekday())
    return timezone.make_aware(datetime.combine(monday, time.min))


def used(user, now=None):
    counted = PrintJob.objects.filter(
        user=user, status__in=PrintJob.COUNTED, submitted_at__gte=week_start(now),
    )
    return counted.aggregate(total=Sum("sides"))["total"] or 0


def banned(user):
    return PrintAccount.objects.filter(user=user, banned=True).exists()


def allowance(user):
    """Сколько сторон в неделю положено. None — без лимита."""
    if user.has_perm(UNLIMITED):
        return None
    own = PrintAccount.objects.filter(user=user).values_list("weekly_limit", flat=True).first()
    return settings.PRINT_WEEKLY_PAGES if own is None else own


def limits(user):
    """Для страницы: сколько потрачено, сколько положено и сколько осталось."""
    spent, total = used(user), allowance(user)
    return {"used": spent, "allowance": total, "left": None if total is None else max(total - spent, 0)}


def lock(user):
    """Занять строку человека до конца транзакции: иначе две вкладки разом обе поставили бы задание."""
    PrintAccount.objects.get_or_create(user=user)
    return PrintAccount.objects.select_for_update().get(user=user)


def _printer(number):
    try:
        return Printer.objects.filter(pk=number).first()
    except (TypeError, ValueError):
        return None


def submit(job, selection, copies, duplex, ip="", printer=None):
    """«Печатать»: проверить выбор и лимит, снять снимок цен, встать в очередь. printer —
    выбранный, иначе принтер задания (все наши — A4)."""
    try:
        copies = int(copies)
    except (TypeError, ValueError):
        raise Refused("Число копий — целое число")
    if not 1 <= copies <= MAX_COPIES:
        raise Refused(f"Копий — от 1 до {MAX_COPIES}")

    with transaction.atomic():
        if lock(job.user).banned:
            raise Refused(BANNED)
        job = PrintJob.objects.select_for_update().select_related("printer").get(pk=job.pk)
        if not job.can_print:
            raise Refused("Это задание уже не ждёт печати — обнови страницу")
        printer = job.printer if printer is None else _printer(printer)
        if printer is None:
            raise Refused("Выбери принтер")
        if not printer.active:
            raise Refused(f"Принтер «{printer}» сейчас не принимает задания")
        if not printer.online:
            raise Refused(f"С принтером «{printer}» нет связи — печать на нём сейчас недоступна")
        job.printer = printer
        try:
            numbers = pricing.pages_of(selection, job.pages)
        except pricing.BadSelection as problem:
            raise Refused(str(problem))
        # Одна страница с двух сторон — та же односторонняя.
        duplex = bool(duplex) and printer.duplex and len(numbers) > 1

        sides = pricing.sides(len(numbers), copies)
        total = allowance(job.user)
        if total is not None:
            left = max(total - used(job.user), 0)
            if sides > left:
                raise Refused(
                    f"Не влезает в недельный лимит: выбрано {sides}, осталось {left}. "
                    "Двусторонняя печать бережёт бумагу, но не лимит — он считается по сторонам."
                )

        costs = printer.costs()
        shares = [job.coverage[number - 1] for number in numbers]
        job.selection = pricing.selection_of(numbers)
        job.copies = copies
        job.duplex = duplex
        job.sides = sides
        job.cost = pricing.cost(shares, copies, duplex, costs)
        job.costs = costs.as_dict()
        job.ip = ip or None
        job.outside_m4 = ip not in settings.PRINT_M4_IPS
        job.status = PrintJob.Status.QUEUED
        job.submitted_at = timezone.now()
        job.reason = ""
        job.tries = 0
        job.save()
    return job


def cancel(job):
    """Отменить, пока агент не взял. Взятое уже уходит в CUPS — его не догнать."""
    with transaction.atomic():
        job = PrintJob.objects.select_for_update().get(pk=job.pk)
        if job.status != PrintJob.Status.QUEUED:
            raise Refused("Задание уже печатается — отменить его нельзя")
        job.status = PrintJob.Status.CANCELED
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
    return job


def again(job):
    """Ещё раз тот же файл — НОВОЕ задание с прежним выбором: напечатанное остаётся в лимите.
    Файлы общие."""
    if banned(job.user):
        raise Refused(BANNED)
    if not job.can_repeat:
        raise Refused("Файл этого задания уже удалён — загрузи его снова")
    return PrintJob.objects.create(
        user=job.user, printer=job.printer, status=PrintJob.Status.READY, name=job.name,
        source_key=job.source_key, pdf_key=job.pdf_key, pages=job.pages, coverage=job.coverage,
        sideways=job.sideways, large=job.large, analyzed_at=job.analyzed_at,
        selection=job.selection, copies=job.copies, duplex=job.duplex,
    )


def remove(job):
    """Удалить задание (не из лимита недели). Файлы — сразу после коммита, если на них не
    смотрит копия «ещё раз»; общую папку уберёт ночь. Агент на доклады получит отказ."""
    with transaction.atomic():
        job = PrintJob.objects.select_for_update().get(pk=job.pk)
        if not job.can_remove:
            raise Refused("Это задание уже в очереди или напечатано — удалить его нельзя")
        folder = job.folder
        job.delete()
        if not PrintJob.objects.filter(source_key__startswith=folder).exists():
            transaction.on_commit(partial(drop_prefix, folder))
