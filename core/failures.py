"""Запись в журнал сбоев (core.models.Failure).

record зовут там, где сбой виден: отказ почты, печати, пекарни. Задачи очереди,
упавшие окончательно, ловит один общий обработчик — так в журнал без единой правки
попадают бэкап, телеграм после повторов и ночные уборки.
"""

import logging
from datetime import timedelta

from celery.signals import task_failure
from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

# Одинаковый сбой в этом окне — та же строка, а не новая.
REPEAT_WINDOW = timedelta(hours=1)
# Эти задачи пишут о себе сами и подробнее, чем сказал бы обработчик: у письма — кому и о чём.
SELF_REPORTING = {"core.tasks.send_email"}


def record(source, text):
    """Записать сбой. Журнал — свидетель, а не участник: своя ошибка тут никогда не
    роняет вызывающего, а точка сохранения не даёт ей отравить его транзакцию."""
    from .models import Failure

    text = str(text)[:500]
    now = timezone.now()
    try:
        with transaction.atomic():
            fresh = Failure.objects.filter(source=source, text=text, last_at__gte=now - REPEAT_WINDOW)
            if not fresh.update(times=F("times") + 1, last_at=now):
                Failure.objects.create(source=source, text=text, created=now, last_at=now)
    except Exception:
        logger.exception("Сбой не записан в журнал: %s — %s", source, text)


@task_failure.connect
def task_failed(sender=None, exception=None, **kwargs):
    """Задача упала, и повторов больше не будет: промежуточные попытки идут через
    Retry и сюда не попадают."""
    from .models import Failure

    name = getattr(sender, "name", str(sender))
    if name not in SELF_REPORTING:
        record(Failure.Source.TASK, f"{name}: {type(exception).__name__}: {exception}")
