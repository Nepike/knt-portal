import logging

from celery.exceptions import OperationalError
from django.template.loader import render_to_string

from .tasks import send_message

logger = logging.getLogger(__name__)

MODERATION = "moderation"  # имя чата в TelegramChat; дальше добавятся support, orders


def notify(chat_name, template, context=None):
    """Собрать сообщение из шаблона и поставить в очередь.

    Рендер здесь, отправка в воркере: в задачу уезжает готовая строка, а модели
    остаются в том процессе, где их достали.

    Если брокер лежит — тихо сдаёмся, в отличие от почты. Уведомление не потеря:
    всё, о чём оно сообщает, и так видно на сайте, а лезть в заблокированный API
    прямо из запроса пользователя — верный способ подвесить страницу.
    """
    text = render_to_string(template, context or {}).strip()
    try:
        send_message.delay(chat_name, text)
    except OperationalError:
        logger.exception("Очередь недоступна, сообщение в «%s» потеряно", chat_name)
