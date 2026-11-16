import logging
import re
import sys
from base64 import b64decode

from celery import shared_task
from django.conf import settings
from requests import RequestException

from .bot import get_bot
from .models import TelegramChat

logger = logging.getLogger(__name__)


CAPTION_LIMIT = 1024  # столько телеграм отводит под подпись к фото
MESSAGE_LIMIT = 4096  # а столько — под сообщение
CUT = "\n… обрезано"


def fit(text, limit=MESSAGE_LIMIT):
    """Уложить в предел телеграма, обрезав с конца: длиннее он не примет. Режим HTML не прощает
    оборванного тега: режем по границе строки, а если она далеко — не посреди тега или &-замены;
    начатое выше обреза закрываем."""
    if len(text) <= limit:
        return text
    cut = text[:limit - len(CUT) - 32]  # запас на закрывающие теги
    line = cut.rfind("\n")
    if line > len(cut) - 400:
        cut = cut[:line]
    else:
        cut = re.sub(r"(<[^>]*|&[#\w]*)$", "", cut)

    opened = []
    for closing, tag in re.findall(r"<(/?)(\w+)[^>]*>", cut):
        if not closing:
            opened.append(tag)
        elif opened and opened[-1] == tag:
            opened.pop()
    return cut + "".join(f"</{tag}>" for tag in reversed(opened)) + CUT


def to_console(chat_name, text):
    """Разработка: печатаем, как письма (core.mail.DevConsoleBackend), — в окне воркера."""
    sys.stdout.write(f"Телеграм → {chat_name}\n\n{text}\n")
    sys.stdout.write("-" * 79 + "\n")


def _target(chat_name):
    """Бот и чат, куда писать, или (None, None), если телеграм не настроен."""
    bot = get_bot()
    chat = TelegramChat.objects.filter(name=chat_name).first()
    if not bot or not chat:
        # Не ошибка: на разработке ни токена, ни чатов нет.
        logger.info("Телеграм не настроен, сообщение в «%s» не отправлено", chat_name)
        return None, None
    return bot, chat


# Повторы недолгие: уведомление не потеря. RETRY_ON_ERROR в telebot выключен — иначе библиотека
# спала бы до 30 с внутри задачи.
@shared_task(autoretry_for=(RequestException,), retry_backoff=True, retry_kwargs={"max_retries": 2})
def send_message(chat_name, text, parse_mode="HTML"):
    """Отправка готового текста; рендер — в веб-процессе (notify)."""
    if settings.TELEGRAM_CONSOLE:
        return to_console(chat_name, text)

    bot, chat = _target(chat_name)
    if bot:
        bot.send_message(
            chat_id=chat.chat_id, message_thread_id=chat.topic_id, text=fit(text), parse_mode=parse_mode,
        )


def _attached(chat_name, text, parse_mode, method, **attachment):
    """Вложение с подписью. Длинный текст — отдельным сообщением, вложение следом."""
    bot, chat = _target(chat_name)
    if not bot:
        return

    where = {"chat_id": chat.chat_id, "message_thread_id": chat.topic_id}
    caption = text if len(text) <= CAPTION_LIMIT else None
    if caption is None:
        bot.send_message(**where, text=fit(text), parse_mode=parse_mode)
    getattr(bot, method)(**where, **attachment, caption=caption, parse_mode=parse_mode)


@shared_task(autoretry_for=(RequestException,), retry_backoff=True, retry_kwargs={"max_retries": 2})
def send_photo(chat_name, text, image, name="image.png", parse_mode="HTML"):
    """Картинка обращения. Приезжает сюда base64-строкой прямо в задаче (см. notify)."""
    if settings.TELEGRAM_CONSOLE:
        return to_console(chat_name, f"{text}\n\n[картинка {name}]")
    _attached(chat_name, text, parse_mode, "send_photo", photo=b64decode(image))


@shared_task(autoretry_for=(RequestException,), retry_backoff=True, retry_kwargs={"max_retries": 2})
def send_document(chat_name, text, document, name, parse_mode="HTML"):
    """Файл как есть (архив аналитики) — base64-строкой в задаче: сайт его не хранит."""
    if settings.TELEGRAM_CONSOLE:
        return to_console(chat_name, f"{text}\n\n[файл {name}]")
    _attached(chat_name, text, parse_mode, "send_document", document=b64decode(document), visible_file_name=name)
