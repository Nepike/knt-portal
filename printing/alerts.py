"""Неполадки принтеров — в телеграм-чат `printers`, только при смене. Зовётся с каждым
показанием агента и раз в минуту задачей (молчание агента замечает сайт).
"""

from django.utils import timezone

from telegram.notify import PRINTERS, notify

from .models import LOW_TONER, OFFLINE_AFTER, Printer, PrintJob

# Ключ → как сказать. В trouble() — по важности: молчание агента, неполадка, тонер на исходе.
# Где неполадку назвал принтер, ключи совпадают с Printer.State.
TROUBLES = {
    "silent": "агент печати молчит — печать стоит",
    "offline": "принтер не отвечает",
    "jam": "замятие бумаги",
    "open": "открыта крышка",
    "no_paper": "нет бумаги",
    "no_toner": "нет тонера",
    "low_toner": "тонер заканчивается",
}


def trouble(printer):
    """Главная неполадка принтера сейчас или пустая строка."""
    if timezone.now() - printer.seen_at > OFFLINE_AFTER:
        return "silent"
    if printer.state in TROUBLES:
        return printer.state
    if printer.toner_percent is not None and printer.toner_percent < LOW_TONER:
        return "low_toner"
    return ""


def check(printer):
    """Написать в чат, если неполадка сменилась; True — написали. Ни разу не отзывавшийся
    и выключенный в админке принтер — не тревога. Условный update: дважды об одном не напишем."""
    if not printer.active or printer.seen_at is None:
        return False
    now, was = trouble(printer), printer.alert
    if now == was or not Printer.objects.filter(pk=printer.pk, alert=was).update(alert=now):
        return False
    printer.alert = now
    notify(PRINTERS, "telegram/printer.html", {
        "printer": printer, "trouble": TROUBLES.get(now, ""), "was": TROUBLES.get(was, ""),
        "waiting": printer.jobs.filter(status__in=PrintJob.ACTIVE).count(),
    })
    return True
