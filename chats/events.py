"""Публикация событий чата в channel layer.

Наружу летит только «в чате N что-то произошло» — без содержимого. Разметку
получатель забирает сам обычным HTTP-запросом, потому что она у каждого своя.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def chat_group(chat_id):
    return f"chat.{chat_id}"


def user_group(user_id):
    return f"user.{user_id}"


def notify_chat(chat_id):
    _publish(chat_group(chat_id), {"type": "chat.event", "chat": chat_id})


def notify_joined(user_id, chat_id):
    """Человека добавили в чат: в группе чата его сокета ещё нет, зовём по личной."""
    _publish(user_group(user_id), {"type": "chat.joined", "chat": chat_id})


def notify_left(user_id, chat_id):
    """Вышел или исключён: иначе сокет остался бы в группе и слушал чужую переписку."""
    _publish(user_group(user_id), {"type": "chat.left", "chat": chat_id})


def _publish(group, payload):
    layer = get_channel_layer()
    if layer is None:  # слой не настроен (например, отдельная management-команда)
        return
    async_to_sync(layer.group_send)(group, payload)
