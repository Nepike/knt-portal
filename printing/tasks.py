from io import StringIO

from celery import shared_task
from django.core.management import call_command

from . import alerts, queue
from .models import Printer


@shared_task
def sweep_printing():
    """Ночная уборка печати: `print_cleanup --apply`.

    Через команду, как sweep_storage: руками её запускают ровно так же. Вывод уходит
    ответом задачи в лог воркера — уборка удаляет файлы и не должна быть молчаливой.
    """
    out = StringIO()
    call_command("print_cleanup", "--apply", stdout=out)
    return out.getvalue().strip()


@shared_task
def watch_printers():
    """Раз в минуту: молчание агента — в чат, а отданное в CUPS и замолчавшее — в «неизвестно».

    И то и другое случается как раз тогда, когда агента нет, — сам он за работой
    не придёт, и никто, кроме сайта, этого не заметит.
    """
    queue.settle()
    for printer in Printer.objects.filter(active=True, seen_at__isnull=False):
        alerts.check(printer)
