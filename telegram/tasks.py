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
    """Уложить сообщение в предел телеграма, обрезав с конца.

    Длиннее предела он не принимает вовсе, и повторять такую ошибку бесполезно —
    уведомление просто пропадало бы. А описание материала или курса длиной не ограничено.

    Обрезать мало: режим HTML не прощает ни оборванного тега, ни незакрытого. Поэтому
    режем по границе строки (тег на две строки не делится), а когда до неё далеко —
    посреди строки, но не посреди тега или &-замены; начатое выше обреза закрываем сами.
    """
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
    """Разработка: бота и чатов нет, но сообщения видеть полезно — печатаем,
    как письма (core.mail.DevConsoleBackend). Задача выполняется в воркере,
    поэтому и печать появится в его окне."""
    sys.stdout.write(f"Телеграм → {chat_name}\n\n{text}\n")
    sys.stdout.write("-" * 79 + "\n")


def _target(chat_name):
    """Бот и чат, куда писать, или (None, None), если телеграм не настроен."""
    bot = get_bot()
    chat = TelegramChat.objects.filter(name=chat_name).first()
    if not bot or not chat:
        # Не ошибка: на разработке ни токена, ни чатов нет, и это нормально.
        logger.info("Телеграм не настроен, сообщение в «%s» не отправлено", chat_name)
        return None, None
    return bot, chat


# Телеграм ходит через прокси в заблокированный API — падений будет хватать.
# Повторяем, но недолго: несостоявшееся уведомление не потеря, всё то же есть на сайте.
# Сетевые ошибки долетают сюда, потому что telebot.apihelper.RETRY_ON_ERROR выключен;
# включить его — значит отдать повторы библиотеке, которая уснёт до 30с внутри задачи.
@shared_task(autoretry_for=(RequestException,), retry_backoff=True, retry_kwargs={"max_retries": 2})
def send_message(chat_name, text, parse_mode="HTML"):
    """Отправка уже готового текста. Рендер остаётся в веб-процессе (см. notify):
    сюда объекты не передать, воркер — отдельный процесс.
    """
    if settings.TELEGRAM_CONSOLE:
        return to_console(chat_name, text)

    bot, chat = _target(chat_name)
    if bot:
        bot.send_message(
            chat_id=chat.chat_id, message_thread_id=chat.topic_id, text=fit(text), parse_mode=parse_mode,
        )


@shared_task(autoretry_for=(RequestException,), retry_backoff=True, retry_kwargs={"max_retries": 2})
def send_photo(chat_name, text, image, name="image.png", parse_mode="HTML"):
    """Картинка обращения. Приезжает сюда base64-строкой прямо в задаче (см. notify).

    Длинный текст телеграм в подпись не пустит — тогда шлём его обычным сообщением,
    а картинку следом: разорванное на две части обращение всё равно лучше обрезанного.
    """
    if settings.TELEGRAM_CONSOLE:
        return to_console(chat_name, f"{text}\n\n[картинка {name}]")

    bot, chat = _target(chat_name)
    if not bot:
        return

    where = {"chat_id": chat.chat_id, "message_thread_id": chat.topic_id}
    caption = text if len(text) <= CAPTION_LIMIT else None
    if caption is None:
        bot.send_message(**where, text=fit(text), parse_mode=parse_mode)
    bot.send_photo(**where, photo=b64decode(image), caption=caption, parse_mode=parse_mode)
