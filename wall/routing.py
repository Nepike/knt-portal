from django.urls import path

from . import consumers

websocket_urlpatterns = [
    path("ws/wall/", consumers.WallConsumer.as_asgi()),
]
