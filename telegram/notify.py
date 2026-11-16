import logging
from base64 import b64encode

from celery.exceptions import OperationalError
from django.template.loader import render_to_string

from .tasks import send_message, send_photo

logger = logging.getLogger(__name__)

# Имена чатов TelegramChat; сами чаты — в админке.
MODERATION = "moderation"
SUPPORT = "support"
# Не поддержка: сюда — про поступление и общежитие, отвечают другие люди.
FEEDBACK = "feedback"
# Неполадки принтеров и молчание агента печати (printing.alerts) — тем, кто носит бумагу.
PRINTERS = "printers"
# Архивы аналитики со второго сервера (telegram/views.py).
ADMIN = "admin"


def notify(chat_name, template, context=None, image=None):
    """Собрать сообщение из шаблона и поставить в очередь. Рендер — здесь, в задачу едет строка;
    картинка — байтами: воркер в другом контейнере. Лежит брокер — тихо сдаёмся: всё то же есть
    на сайте, а ждать заблокированный API из запроса нельзя."""
    text = render_to_string(template, context or {}).strip()
    try:
        if image:
            image.seek(0)
            send_photo.delay(chat_name, text, b64encode(image.read()).decode(), image.name)
        else:
            send_message.delay(chat_name, text)
    except OperationalError:
        logger.exception("Очередь недоступна, сообщение в «%s» потеряно", chat_name)
