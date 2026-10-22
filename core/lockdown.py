"""Заслон: сайт закрыт для всех, кроме белого списка.

Включается записью `core.Lockdown` — в админке или командой `manage.py lockdown`.
Пока она действует, любой адрес отвечает одной страницей (`core/lockdown.html`):
и гостю, и вошедшему. Проходят те, у кого есть право `core.pass_lockdown`, — его
несёт группа «Доступ при закрытии»; суперпользователю оно не нужно.

Страницы закрывает `core.middleware.LockdownMiddleware`, сокеты — `SocketGate`
в `knt/asgi.py`. Сокет, открытый ДО закрытия, доживает до первого переподключения:
по нему летит только повод, а разметку вкладка забирает запросом — и уже не получит.
"""

from channels.db import database_sync_to_async
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import Resolver404, resolve
from django.utils.cache import add_never_cache_headers

from .models import Lockdown

PASS = "core.pass_lockdown"
EDIT = "core.change_lockdown"
GROUP = "Доступ при закрытии"

# Что работает и при закрытом сайте. Имена урлов, а не пути — как в core/nav.py.
OPEN = {
    # Дверь для белого списка: без входа ему не попасть внутрь, а выход и смена
    # пароля нужны тому, кто вошёл не под тем именем или впервые.
    "login", "logout", "password_change",
    "password_reset", "password_reset_done", "password_reset_confirm", "password_reset_complete",
    # Раздача файлов. На домене файлов сессии нет, и отличить там белый список от
    # остальных нечем: закрыть — значит оставить без картинок и видео и его тоже.
    # Разрешением служит подпись в адресе, а новых адресов без страниц не получить.
    "file_download", "media_image", "hls_piece",
}
# Машины со своими токенами — пекарня и агент печати: отказ они приняли бы за поломку.
# Кортеж: str.startswith принимает его как «любой из».
OPEN_PREFIX = ("intake_", "printagent_")

# Сколько просим поисковик не возвращаться. Человеку этот заголовок не виден.
RETRY_AFTER = 3600


def always_open(path):
    try:
        name = resolve(path).url_name or ""
    except Resolver404:
        return False
    return name in OPEN or name.startswith(OPEN_PREFIX)


def current():
    """Действующий заслон или None, если сайт открыт."""
    return Lockdown.objects.filter(pk=1, active=True).first()


def lets_in(user):
    return user.is_authenticated and user.has_perm(PASS)


def page(request, lock, status=200):
    return render(request, "core/lockdown.html", {"lock": lock}, status=status)


def refusal(request, lock):
    if request.headers.get("HX-Request"):
        # Страница закрытия, вставленная куском в чужой список, никому не нужна:
        # пусть вкладка перечитает себя целиком и получит её как положено.
        return HttpResponse(headers={"HX-Refresh": "true"})
    # 503, а не 200: поисковик не примет этот текст за содержимое «О сайте».
    response = page(request, lock, status=503)
    response["Retry-After"] = RETRY_AFTER
    # Иначе после открытия браузер мог бы достать страницу закрытия из своего кеша.
    add_never_cache_headers(response)
    return response


@database_sync_to_async
def _shut_for(user):
    return current() is not None and not lets_in(user)


class SocketGate:
    """Заслон для сокетов. Стоит ВНУТРИ AuthMiddlewareStack: ему нужен уже узнанный человек."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if await _shut_for(scope["user"]):
            # Отказ — ответом на рукопожатие, как делает и консьюмер: до accept
            # закрытие сокета и значит «не пустили».
            await receive()
            await send({"type": "websocket.close"})
            return
        await self.inner(scope, receive, send)
