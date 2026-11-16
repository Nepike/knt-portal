"""Ручки для машин: пекарни (`intake`) и агента печати (`printing`).

`@login_not_required` и свой токен у каждой машины (`Authorization: Bearer …`). Ответ —
JSON с текстом причины: на той стороне скрипт, ему нужен внятный текст, а не «401».
"""

import json
import secrets

from django.conf import settings
from django.http import JsonResponse

BEARER = "Bearer "


def unusable(token, setting):
    """Почему токеном пользоваться нельзя, или None. setting — имя настройки.

    Заголовок HTTP несёт только ASCII без пробелов. Пробел обычно — хвост `# заметка`,
    который django-environ оставляет в значении.
    """
    if not token:
        return f"ручки не настроены: пустой {setting}"
    if not token.isascii() or not token.isprintable() or " " in token:
        return f"{setting} годится только из латиницы, цифр и знаков, без пробелов: заголовки HTTP другого не несут"
    return None


def authorised(request, token):
    header = request.headers.get("Authorization", "")
    if not header.startswith(BEARER):
        return False
    # compare_digest — против подбора по времени ответа. Байтами: строковый вариант падает
    # на не-ASCII, а заголовок — чужой ввод.
    return secrets.compare_digest(header[len(BEARER):].encode(), token.encode())


def guard(request, setting):
    """Ответ-отказ или None, если пускаем. Ненастроенный токен — отказ, а не «пускаем всех»."""
    token = getattr(settings, setting)
    if problem := unusable(token, setting):
        return JsonResponse({"error": problem}, status=503)
    if not authorised(request, token):
        return JsonResponse({"error": f"нужен заголовок Authorization: Bearer <{setting}>"}, status=401)
    return None


def body(request):
    """Тело запроса машины. Не JSON или не объект — пусто: дальше разберётся проверка полей."""
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
