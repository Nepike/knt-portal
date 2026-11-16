"""Заслон: сайт закрыт для всех, кроме права `core.pass_lockdown` (группа «Доступ при закрытии»).

Включается записью `core.Lockdown` — в админке или `manage.py lockdown`. Страницы закрывает
`LockdownMiddleware`, сокеты — `SocketGate` в `knt/asgi.py`. Открытый до закрытия сокет
доживает до переподключения: по нему летит только повод, а разметку вкладка уже не получит.
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
    # Дверь для белого списка; выход и смена пароля — тому, кто вошёл не тем или впервые.
    "login", "logout", "password_change",
    "password_reset", "password_reset_done", "password_reset_confirm", "password_reset_complete",
    # Раздача файлов: на домене файлов сессии нет. Разрешение — подпись в адресе.
    "file_download", "media_image", "hls_piece",
    # Проверка деплоя: выкладка во время закрытия иначе считалась бы упавшей.
    "healthz",
}
# Машины со своими токенами: отказ они приняли бы за поломку. Кортеж — для str.startswith.
OPEN_PREFIX = ("intake_", "printagent_", "analytics_")

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
        # Кусок страницы закрытия в чужом списке не нужен — вкладка перечитает себя целиком.
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
            # Отказ на рукопожатие: закрытие до accept и значит «не пустили».
            await receive()
            await send({"type": "websocket.close"})
            return
        await self.inner(scope, receive, send)
