"""Ограничитель частоты для открытых форм: вход, восстановление пароля, поддержка.
Счёт — в кэше, на проде общем для всех воркеров (Redis).
"""

import ipaddress
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)


def throttled(key, limit, window=3600):
    """Фиксированное окно: True — лимит исчерпан."""
    try:
        return _count(key, window) > limit
    except Exception:  # noqa: BLE001 — какой именно, зависит от бекенда кэша
        # Кэш лёг — пропускаем и пишем в лог: ограничитель не должен закрыть вход всем.
        logger.exception("Кэш недоступен, ограничитель пропускает «%s»", key)
        return False


def exhausted(key, limit):
    """Лимит уже выбран; сама проверка не в счёт (пара к strike: считают только неудачи)."""
    try:
        return (cache.get(key) or 0) >= limit
    except Exception:  # noqa: BLE001
        logger.exception("Кэш недоступен, ограничитель пропускает «%s»", key)
        return False


def strike(key, window=3600):
    """Засчитать неудачу в окно."""
    try:
        _count(key, window)
    except Exception:  # noqa: BLE001
        logger.exception("Кэш недоступен, неудача по «%s» не засчитана", key)


def forgive(key):
    try:
        cache.delete(key)
    except Exception:  # noqa: BLE001
        logger.exception("Кэш недоступен, счёт «%s» не сброшен", key)


def _count(key, window):
    """Номер вызова в текущем окне. Окно заводит cache.add (он и ставит срок); incr срок
    не продлевает — окно фиксированное."""
    if cache.add(key, 1, window):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        # Ключ истёк между add и incr — значит окно уже закончилось, начинаем новое.
        cache.set(key, 1, window)
        return 1


def client_ip(request):
    """Адрес клиента, каким его видел наш nginx. X-Real-IP nginx ставит сам; первый адрес
    в X-Forwarded-For прислал клиент, ему верить нельзя. Без nginx (dev) — адрес соединения."""
    for raw in (request.META.get("HTTP_X_REAL_IP"), request.META.get("REMOTE_ADDR")):
        try:
            return str(ipaddress.ip_address((raw or "").strip()))
        except ValueError:
            continue
    return ""
