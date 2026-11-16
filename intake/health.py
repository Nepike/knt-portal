"""Здоровье пекарни: её неполадки и её молчание — в чат admin и в журнал сбоев.

В чат — только смена состояния, как у принтеров (printing/alerts.py); журнал сбоев пишется
каждый раз и сам сводит одинаковое за час в строку со счётчиком.
"""

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Max, Q
from django.utils import timezone

from core.failures import record
from core.models import Failure
from telegram.notify import ADMIN, notify

from .models import Bakery, MediaJob

# Тревога о молчании — только когда работа стоит: запись ждёт дольше WAIT_ALARM, а пекарни
# молчат дольше SILENT_AFTER. Пустая очередь и выключенный на ночь компьютер — не повод.
WAIT_ALARM = timedelta(minutes=30)
# Пекарня спрашивает работу раз в минуту, а за работой стучит раз в пять (bake.BEAT).
SILENT_AFTER = timedelta(minutes=15)
# О молчании уже сказано. В кэше: молчат все машины разом; пропал кэш — одно лишнее сообщение.
SILENCE = "intake:silence"


def heard(name, problem=""):
    """Пекарня вышла на связь — с неполадкой или без. Сменилась неполадка — в чат."""
    name, problem = (name or "пекарня")[:100], problem[:300]
    bakery, _ = Bakery.objects.update_or_create(
        name=name, defaults={"seen_at": timezone.now(), "problem": problem},
    )
    if problem:
        record(Failure.Source.BAKERY, f"{name}: {problem}")
    was = bakery.alert
    # Условным update: два запроса разом не напишут об одном и том же дважды.
    if problem != was and Bakery.objects.filter(pk=bakery.pk, alert=was).update(alert=problem):
        bakery.alert = problem
        notify(ADMIN, "telegram/bakery.html", {
            "bakery": bakery, "problem": problem, "was": was, "waiting": stuck().count(),
        })
    return bakery


def stuck(now=None):
    """Записи, которые стоят: ждут в очереди или взяты, но о них давно ни слова."""
    edge = (now or timezone.now()) - WAIT_ALARM
    return MediaJob.objects.filter(
        Q(status=MediaJob.Status.WAITING, created__lt=edge)
        | Q(status=MediaJob.Status.BAKING, claimed_at__lt=edge)
    )


def silent():
    """Сказано ли сейчас, что пекарни молчат, — для Пульса."""
    return bool(cache.get(SILENCE))


def watch(now=None):
    """Раз в пять минут (intake.tasks.watch_bakeries): записи стоят, а пекарни молчат —
    о своём молчании пекарня сообщить не может."""
    now = now or timezone.now()
    count = stuck(now).count()
    last = Bakery.objects.aggregate(last=Max("seen_at"))["last"]
    quiet = bool(count) and (last is None or now - last >= SILENT_AFTER)
    if quiet:
        record(Failure.Source.BAKERY, "записи ждут, а пекарня не выходит на связь")
    if quiet != silent():
        cache.set(SILENCE, quiet, timeout=None)
        notify(ADMIN, "telegram/bakery_silent.html", {"silent": quiet, "waiting": count, "last": last})
    return quiet
