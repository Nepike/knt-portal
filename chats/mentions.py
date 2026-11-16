"""Упоминания: кого позвали в сообщении. Считаем по тексту (переживает правку, не
подделать), «Фамилия Имя» — как User.full_name.
"""

from users.models import User

# Дешёвая отсечка: без собачки базу не спрашиваем.
MARK = "@"


def mentioned(chat, text):
    """Участники чата, чьё имя названо в тексте после собачки."""
    if MARK not in text:
        return []
    people = User.objects.filter(chat_memberships__chat=chat).only("name", "surname")
    return [person for person in people if f"{MARK}{person.full_name}" in text]


def remember(message):
    """Переписать упоминания сообщения по его тексту. Зовётся и при отправке, и при правке."""
    message.mentions.set(mentioned(message.chat, message.text))
