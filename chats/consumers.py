from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .events import chat_group, user_group
from .models import Membership

# Что из события уходит в браузер. Не всё подряд: `type` — служебный ключ channels,
# по нему он выбирает метод, и в окне браузера ему делать нечего.
PAYLOAD = ("chat", "msg", "author", "read", "by", "kind")


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """Один сокет на вкладку, а не на открытый чат.

    По сокету приходит только событие изменений в чате,
    а сам HTML догружается обычным запросом, так как разметка у всех уникальна.
    """

    async def connect(self):
        self.user = self.scope["user"]
        if not self.user.is_authenticated:
            await self.close()
            return
        self.joined = []
        await self.subscribe()
        await self.accept()

    async def disconnect(self, code):
        for group in getattr(self, "joined", []):
            await self.channel_layer.group_discard(group, self.channel_name)

    async def subscribe(self):
        wanted = [user_group(self.user.pk)] + [chat_group(pk) for pk in await self.chat_ids()]
        for group in wanted:
            if group not in self.joined:
                await self.channel_layer.group_add(group, self.channel_name)
                self.joined.append(group)

    @database_sync_to_async
    def chat_ids(self):
        return list(Membership.objects.filter(user=self.user).values_list("chat_id", flat=True))

    # --- сообщения из channel layer (ключ type определяет имя метода) ---

    async def chat_event(self, event):
        await self.send_json({key: event[key] for key in PAYLOAD if key in event})

    async def chat_joined(self, event):
        """Появился новый чат — досоединяемся к его группе и обновляем список."""
        await self.subscribe()
        await self.send_json({"chat": event["chat"]})

    async def chat_left(self, event):
        group = chat_group(event["chat"])
        if group in self.joined:
            await self.channel_layer.group_discard(group, self.channel_name)
            self.joined.remove(group)
        await self.send_json({"chat": event["chat"]})  # чат пропал — обновить список
