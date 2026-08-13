"""ASGI-вход: HTTP отдаём Django как раньше, WebSocket уводим в Channels."""

import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knt.settings.dev")

# Приложения должны подняться ДО импорта роутинга: он тянет консьюмеры, а те — модели.
django_asgi_app = get_asgi_application()

from chats.routing import websocket_urlpatterns as chat_sockets  # noqa: E402
from wall.routing import websocket_urlpatterns as wall_sockets  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    # CSRF для сокетов: same-origin на WebSocket не распространяется, и без проверки
    # Origin чужая страница открыла бы соединение с cookie пользователя.
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter(chat_sockets + wall_sockets))
    ),
})
