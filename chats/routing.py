from django.urls import path

from . import consumers

websocket_urlpatterns = [
    path("ws/chats/", consumers.ChatConsumer.as_asgi()),
]
