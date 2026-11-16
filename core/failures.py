"""Журнал сбоев (core.models.Failure).

`record` зовут места сбоев: почта, печать, пекарня. Окончательно упавшие задачи очереди
и пятисотки пишут общие обработчики сигналов.
"""

import logging
import sys
from datetime import timedelta

from celery.signals import task_failure
from django.core.signals import got_request_exception
from django.db import transaction
from django.db.models import F
from django.dispatch import receiver
from django.urls import resolve
from django.utils import timezone

logger = logging.getLogger(__name__)

# Одинаковый сбой в этом окне — та же строка, а не новая.
REPEAT_WINDOW = timedelta(hours=1)
# Эти задачи пишут о себе сами, подробнее обработчика.
SELF_REPORTING = {"core.tasks.send_email"}


def record(source, text):
    """Записать сбой. Своя ошибка не роняет вызывающего; точка сохранения бережёт его транзакцию."""
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


@receiver(got_request_exception)
def request_failed(sender=None, request=None, **kwargs):
    """Запрос упал пятисоткой. Сигнал идёт только о необработанном исключении (404 и 403 —
    нет) и изнутри except: sys.exc_info() — само исключение, и под ASGI тоже."""
    from .models import Failure

    try:
        error = sys.exc_info()[1]
        record(Failure.Source.WEB, f"{request.method} {_route(request)}: {type(error).__name__}: {error}")
    except Exception:
        # Исключение из обработчика сигнала сломало бы ответ: вместо страницы 500 — обрыв.
        logger.exception("Пятисотка не записана в журнал")


def _route(request):
    """Шаблон адреса, а не путь: одна ошибка на сотне страниц — одна строка. И в пути
    бывают токены."""
    match = getattr(request, "resolver_match", None)
    if match is None:
        # Упало в middleware, до разбора адреса.
        try:
            match = resolve(request.path_info)
        except Exception:
            return "(адрес не из urls)"
    return "/" + match.route
