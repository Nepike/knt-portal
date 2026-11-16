import logging
from smtplib import SMTPServerDisconnected

from celery.exceptions import OperationalError
from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.console import EmailBackend

logger = logging.getLogger(__name__)

ALERT_EVERY = 3600  # секунд между сообщениями о сбое почты


class DevConsoleBackend(EmailBackend):
    """Печатает письмо в читаемом виде — стандартный console-бекенд выводит сырой MIME (base64 для кириллицы)."""

    def write_message(self, message):
        self.stream.write(f"От: {message.from_email}\nКому: {', '.join(message.to)}\nТема: {message.subject}\n\n{message.body}\n")
        self.stream.write("-" * 79 + "\n")


def pack(message):
    """Письмо → словарь для JSON: воркер — отдельный процесс."""
    if message.attachments:
        # TODO: вложения — base64; тело задачи лежит в Redis, мегабайты туда не класть.
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


def report_failure(payload, error):
    """Письмо не ушло ни с одной попытки. В лог — каждое, в телеграм — первое за час:
    почта ломается разом. Тела письма в сообщении нет: в нём ссылка для пароля."""
    from telegram.notify import SUPPORT, notify

    from .failures import record
    from .models import Failure

    to = ", ".join(payload["to"])
    logger.error("Письмо не ушло: кому %s, тема «%s»: %r", to, payload["subject"], error)
    record(Failure.Source.MAIL, f"кому {to}, тема «{payload['subject']}»: {type(error).__name__}: {error}")
    try:
        if cache.add("mail:failed", 1, ALERT_EVERY):
            notify(SUPPORT, "telegram/mail_failed.html", {
                "to": to,
                "subject": payload["subject"],
                "error": f"{type(error).__name__}: {error}",
                # Gmail на неверный пароль отвечает 535 и закрывает соединение, а smtplib
                # пробует второй способ входа по закрытому — и в ошибке остаётся только обрыв.
                "refused": isinstance(error, SMTPServerDisconnected),
            })
    except Exception:
        # Сообщение о сбое не должно заслонить сам сбой: задача упадёт с ошибкой почты.
        logger.exception("Не получилось сообщить о сбое почты")


class QueuedEmailBackend(BaseEmailBackend):
    """Письма уходят в очередь, а не в SMTP: запрос не ждёт чужой сервер. Бекендом — чтобы
    через очередь шли и встроенные письма Django (сброс пароля, приглашение)."""

    def send_messages(self, messages):
        from .tasks import send_email

        for message in messages:
            payload = pack(message)
            try:
                send_email.delay(payload)
            except OperationalError:
                # Брокер лёг: письмо со ссылкой на вход важнее скорости — отправляем здесь.
                logger.exception("Очередь недоступна, отправляю письмо напрямую")
                deliver(payload)
        return len(messages)
