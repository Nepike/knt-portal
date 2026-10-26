"""Последний запуск задач очереди — для «Системы» Пульса.

Кэш, а не таблица: нужна одна отметка на задачу, история не нужна. Неделя хранения
покрывает и ночные задачи, и ежеминутные. Отметку ставит воркер по сигналу Celery, так
что новая задача по расписанию попадает сюда сама.
"""

import logging

from celery.signals import task_postrun
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

KEEP = 7 * 24 * 3600


def _key(name):
    return f"run:{name}"


@task_postrun.connect
def ran(sender=None, retval=None, state=None, **kwargs):
    name = getattr(sender, "name", "")
    try:
        cache.set(_key(name), {"at": timezone.now(), "ok": state == "SUCCESS", "result": str(retval)[:200]}, KEEP)
    except Exception:  # noqa: BLE001 — кэш лёг; задача от этого не должна падать
        logger.exception("Не записан последний запуск задачи %s", name)


def last(name):
    """{"at", "ok", "result"} последнего запуска или None — не запускалась неделю или кэш недоступен."""
    try:
        return cache.get(_key(name))
    except Exception:  # noqa: BLE001
        logger.exception("Кэш недоступен, последний запуск %s не прочитан", name)
        return None
