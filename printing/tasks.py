from io import StringIO

from celery import shared_task
from django.core.management import call_command

from . import alerts, queue
from .models import Printer


@shared_task
def sweep_printing():
    """Ночная уборка печати: `print_cleanup --apply`, как sweep_storage; вывод — в лог воркера."""
    out = StringIO()
    call_command("print_cleanup", "--apply", stdout=out)
    return out.getvalue().strip()


@shared_task
def watch_printers():
    """Раз в минуту: молчание агента — в чат, отданное в CUPS и замолчавшее — в «неизвестно».
    Без агента этого никто, кроме сайта, не заметит."""
    queue.settle()
    for printer in Printer.objects.filter(active=True, seen_at__isnull=False):
        alerts.check(printer)
