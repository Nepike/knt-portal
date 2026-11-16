"""События чата в channel layer. Наружу летит повод, а не разметка: пузырь у каждого свой.
О новом сообщении событие говорит ещё, чьё оно и его id — счётчик вкладка правит сама.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def chat_group(chat_id):
    return f"chat.{chat_id}"


def user_group(user_id):
    return f"user.{user_id}"


def notify_chat(chat_id, message=None, kind=None):
    """`message` — новое сообщение, иначе `kind` — что изменилось: реакция, правка или
    удаление трогают разное (разбирает components.js)."""
    event = {"type": "chat.event", "chat": chat_id}
    if message is not None:
        event["msg"] = message.pk
        event["author"] = message.author_id or 0  # 0 — служебная строка, автора у неё нет
    if kind:
        event["kind"] = kind
    _publish(chat_group(chat_id), event)


def notify_read(chat_id, user_id, upto_id):
    """Собеседник дочитал до `upto_id`. Только для ЛС: в группе это была бы рассылка
    курсора каждого каждому; там прочтение спрашивают по нажатию."""
    _publish(chat_group(chat_id), {"type": "chat.event", "chat": chat_id, "read": upto_id, "by": user_id})


def notify_joined(user_id, chat_id):
    """Человека добавили в чат: в группе чата его сокета ещё нет, зовём по личной."""
    _publish(user_group(user_id), {"type": "chat.joined", "chat": chat_id})


def notify_left(user_id, chat_id):
    """Вышел или исключён: иначе сокет остался бы в группе и слушал чужую переписку."""
    _publish(user_group(user_id), {"type": "chat.left", "chat": chat_id})


def notify_notifications(*user_ids):
    """Колокольчик: у людей изменились уведомления — их вкладки переспросят число. Повод идёт
    в личную группу. Объявление — сотням разом, поэтому один проход по слою."""
    layer = get_channel_layer()
    if layer is None or not user_ids:
        return

    async def send():
        for user_id in user_ids:
            await layer.group_send(user_group(user_id), {"type": "notice.changed"})

    async_to_sync(send)()


def _publish(group, payload):
    layer = get_channel_layer()
    if layer is None:  # слой не настроен (например, отдельная management-команда)
        return
    async_to_sync(layer.group_send)(group, payload)
