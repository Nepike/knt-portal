import logging
from base64 import b64encode

from celery.exceptions import OperationalError
from django.template.loader import render_to_string

from .tasks import send_message, send_photo

logger = logging.getLogger(__name__)

# Имена чатов в TelegramChat. Сам чат заводится в админке — chat_id узнаётся только
# у живого чата (команда /get_chat_id, см. management/commands/bot.py).
MODERATION = "moderation"
SUPPORT = "support"


def notify(chat_name, template, context=None, image=None):
    """Собрать сообщение из шаблона и поставить в очередь.

    Рендер здесь, отправка в воркере: в задачу уезжает готовая строка, а модели
    остаются в том процессе, где их достали. Картинка (её прикладывают к обращению
    в поддержку) уезжает туда же байтами — воркер живёт в отдельном контейнере,
    и файла на диске веб-процесса он не увидит.

    Если брокер лежит — тихо сдаёмся, в отличие от почты. Уведомление не потеря:
    всё, о чём оно сообщает, и так видно на сайте, а лезть в заблокированный API
    прямо из запроса пользователя — верный способ подвесить страницу.
    """
    text = render_to_string(template, context or {}).strip()
    try:
        if image:
            image.seek(0)
            send_photo.delay(chat_name, text, b64encode(image.read()).decode(), image.name)
        else:
            send_message.delay(chat_name, text)
    except OperationalError:
        logger.exception("Очередь недоступна, сообщение в «%s» потеряно", chat_name)
