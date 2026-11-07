"""Здоровье пекарни: её собственные неполадки и её молчание — в чат admin и в журнал сбоев.

Как у принтеров (printing/alerts.py), в чат пишем только смену: беда, стоящая сутки,
иначе дала бы полторы тысячи сообщений. Журнал сбоев пишется каждый раз — одинаковое
за час он сам сводит в одну строку со счётчиком, и в Сводке Пульса видно, что беда длится.

Повод — 8 ноя 2026: NVENC на М4 не работал неизвестно сколько, а узнали об этом по чужой
задаче. Сданная в это время лекция упала бы отказом, и никто, кроме её автора, не увидел бы.
"""

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Max, Q
from django.utils import timezone

from core.failures import record
from core.models import Failure
from telegram.notify import ADMIN, notify

from .models import Bakery, MediaJob

# Тревога о молчании — только когда работа правда стоит: запись ждёт дольше WAIT_ALARM,
# а ни одна пекарня не выходила на связь SILENT_AFTER. Пустая очередь и выключенный на ночь
# домашний компьютер — не повод: иначе сообщение приходило бы каждый вечер.
WAIT_ALARM = timedelta(minutes=30)
# Пекарня спрашивает работу раз в минуту, а за работой стучит раз в пять (bake.BEAT).
SILENT_AFTER = timedelta(minutes=15)
# О молчании уже сказано. В кэше, а не в базе: машины тут ни при чём — молчат все разом.
# Пропал кэш — худшее, что будет, это одно повторное сообщение.
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
    """Раз в пять минут (intake.tasks.watch_bakeries): записи стоят, а пекарни молчат.

    О своём молчании пекарня сообщить не может — так 4 дня в сентябре 2026 М4 была без
    сети, и это не было видно ниоткуда.
    """
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
