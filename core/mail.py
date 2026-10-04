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


def report_failure(payload, error):
    """Письмо не ушло ни с одной попытки — сказать об этом людям.

    В лог идёт каждое: по нему видно, кому писать заново. В телеграм — первое за час:
    почта ломается вся разом, и регистрация курса ведомостью дала бы сотню одинаковых
    сообщений. Тела письма в сообщении нет: в нём ссылка, по которой задают пароль.
    """
    from telegram.notify import SUPPORT, notify

    to = ", ".join(payload["to"])
    logger.error("Письмо не ушло: кому %s, тема «%s»: %r", to, payload["subject"], error)
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
    """Письма уходят в очередь, а не в SMTP: запрос пользователя не должен ждать
    чужой сервер (gmail по SSL — это секунды, а воркеров у gunicorn всего два).

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
