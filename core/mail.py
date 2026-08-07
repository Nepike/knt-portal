import logging

from celery.exceptions import OperationalError
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.console import EmailBackend

logger = logging.getLogger(__name__)


class DevConsoleBackend(EmailBackend):
    """Печатает письмо в читаемом виде — стандартный console-бекенд выводит сырой MIME (base64 для кириллицы)."""

    def write_message(self, message):
        self.stream.write(f"От: {message.from_email}\nКому: {', '.join(message.to)}\nТема: {message.subject}\n\n{message.body}\n")
        self.stream.write("-" * 79 + "\n")


def pack(message):
    """Письмо → словарь, который переживёт JSON и дорогу до воркера.

    Сам объект передать нельзя: воркер — отдельный процесс со своей памятью.
    """
    if message.attachments:
        # TODO: понадобятся вложения — класть их base64; помнить, что тело задачи
        # целиком лежит в Redis, и мегабайтные файлы туда пихать не стоит.
        raise ValueError("Письма с вложениями через очередь пока не отправляются")
    return {
        "subject": message.subject,
        "body": message.body,
        "from_email": message.from_email,
        "to": message.to,
        "cc": message.cc,
        "bcc": message.bcc,
        "reply_to": message.reply_to,
        "headers": message.extra_headers,
        "alternatives": [[part.content, part.mimetype] for part in message.alternatives],
    }


def deliver(payload):
    """Собрать письмо обратно и отправить по-настоящему. Зовётся уже в воркере."""
    connection = get_connection(settings.EMAIL_DELIVERY_BACKEND)
    return EmailMultiAlternatives(connection=connection, **payload).send()


class QueuedEmailBackend(BaseEmailBackend):
    """Письма уходят в очередь, а не в SMTP: запрос пользователя не должен ждать
    чужой сервер (gmail по SSL — это секунды, а воркеров у gunicorn всего три).

    Сделано именно бекендом, а не задачей во вьюхах: так через очередь идут и
    встроенные письма Django — сброс пароля и приглашение при регистрации.
    """

    def send_messages(self, messages):
        from .tasks import send_email

        for message in messages:
            payload = pack(message)
            try:
                send_email.delay(payload)
            except OperationalError:
                # Брокер лёг. Письмо со ссылкой на вход важнее скорости ответа —
                # отправляем прямо здесь, пусть человек подождёт.
                logger.exception("Очередь недоступна, отправляю письмо напрямую")
                deliver(payload)
        return len(messages)
