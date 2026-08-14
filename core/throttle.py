"""Ограничитель частоты запросов.

Живёт в core, а не рядом с одним из пользователей: им пользуются и восстановление пароля,
и поддержка — обе формы открыты без логина, то есть доступны кому угодно из интернета.
"""

from django.core.cache import cache


def throttled(key, limit, window=3600):
    """Фиксированное окно на кэше: True — лимит исчерпан."""
    if cache.add(key, 1, window):
        return False
    try:
        return cache.incr(key) > limit
    except ValueError:
        cache.set(key, 1, window)
        return False


def client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "")
