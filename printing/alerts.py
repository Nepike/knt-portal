"""Неполадки принтеров — сообщением в телеграм-чат `printers`, только при смене.

Зовётся с каждым показанием агента (`printing.agent.status`) и раз в минуту задачей
`printing.tasks.watch_printers`: о своём молчании агент сообщить не может, его замечает сайт.
"""

from django.utils import timezone

from telegram.notify import PRINTERS, notify

from .models import LOW_TONER, OFFLINE_AFTER, Printer, PrintJob

# Ключ → как сказать. Порядок проверки в trouble() — от важного: молчащий агент важнее
# всего, что он рассказал раньше, а неполадка принтера — важнее тонера на исходе.
# Ключи совпадают с Printer.State там, где неполадку назвал принтер.
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
    """Написать в чат, если неполадка сменилась. True — написали.

    Принтер, с которого агент ещё ни разу не приходил, молчит не тревожно: его только
    завели в админке. Выключенный в админке — тоже не тревога, его вывели нарочно.
    Смену ставим условным update: два показания разом не напишут об одном дважды.
    """
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
