"""Запись в журнал сбоев (core.models.Failure).

record зовут там, где сбой виден: отказ почты, печати, пекарни. Задачи очереди,
упавшие окончательно, ловит один общий обработчик — так в журнал без единой правки
попадают бэкап, телеграм после повторов и ночные уборки. Так же, одним обработчиком,
сюда попадают запросы, упавшие пятисоткой.
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


@receiver(got_request_exception)
def request_failed(sender=None, request=None, **kwargs):
    """Запрос упал пятисоткой. До журнала она оставалась только в логе контейнера.

    Django шлёт этот сигнал лишь о необработанном исключении: 404, 403 и 400 сюда не
    приходят, и сканеры журнал не засорят. Сигнал шлётся изнутри except, поэтому
    sys.exc_info() здесь — само исключение (и под ASGI тоже: asgiref переносит его в поток).
    """
    from .models import Failure

    try:
        error = sys.exc_info()[1]
        record(Failure.Source.WEB, f"{request.method} {_route(request)}: {type(error).__name__}: {error}")
    except Exception:
        # Исключение из обработчика сигнала сломало бы ответ: вместо страницы 500 — обрыв.
        logger.exception("Пятисотка не записана в журнал")


def _route(request):
    """Шаблон адреса, а не сам путь: одна ошибка на сотне материалов ложится одной строкой
    со счётчиком. Да и в пути бывают токены (раздача файлов), а строки запроса нет вовсе."""
    match = getattr(request, "resolver_match", None)
    if match is None:
        # Упало в middleware, до разбора адреса.
        try:
            match = resolve(request.path_info)
        except Exception:
            return "(адрес не из urls)"
    return "/" + match.route
