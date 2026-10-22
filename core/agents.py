"""Ручки для машин: пекарни (`intake`) и агента печати (`printing`).

Машине логин не годится — у неё нет ни браузера, ни сессии. Поэтому `@login_not_required`
и свой токен в заголовке `Authorization: Bearer …`, у каждой машины отдельный: пекарню
и печать отзывают независимо. Ответ — JSON с текстом причины: на той стороне скрипт,
который должен напечатать внятное, а не «401».
"""

import json
import secrets

from django.conf import settings
from django.http import JsonResponse

BEARER = "Bearer "


def unusable(token, setting):
    """Почему таким токеном пользоваться нельзя, или None. setting — имя настройки.

    Заголовки HTTP переносят только латиницу: кириллический токен клиент физически
    не отправит, а пробел разорвёт заголовок. Отвергаем это КАК ненастроенность —
    иначе токен «работал бы» в тестах, а машина падала бы на кодировке.

    Пробел ловится не для красоты: django-environ не срезает хвост после решётки,
    и `INTAKE_TOKEN=abc # заметка` даёт значение вместе с заметкой (на это в проекте
    уже наступали). Такой токен лучше назвать сломанным сразу.
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
    # compare_digest, а не ==: сравнение строк выходит на первом несовпавшем символе,
    # и по времени ответа токен подбирается посимвольно.
    #
    # Байты, а не строки: строковый вариант отказывается сравнивать что угодно за
    # пределами латиницы, а ЗАГОЛОВОК — это чужой ввод. Настройка своя и проверена
    # в unusable(), но присланное проверить нельзя, и падать на нём вьюха не должна.
    return secrets.compare_digest(header[len(BEARER):].encode(), token.encode())


def guard(request, setting):
    """Ответ-отказ или None, если пускаем.

    Ненастроенность — это НЕ «пускаем всех»: ручки открыты в интернет без логина,
    и забытая или битая настройка не должна превращаться в открытую дверь.
    """
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
