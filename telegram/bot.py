"""Один на всё приложение клиент telebot.

Собираем лениво: без токена (так живёт разработка) бота просто нет, и всё, что
через него ходит, тихо выключается — сайт от этого работать не перестаёт.
"""

import telebot
from django.conf import settings
from telebot import apihelper


def get_bot():
    """Клиент или None, если бот не настроен."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return None
    # api.telegram.org заблокирован в РФ — с сервера ходим через прокси.
    # Настройка модульная, поэтому ставим её здесь, до первого запроса.
    apihelper.proxy = {"https": settings.PROXY} if settings.PROXY else None
    return telebot.TeleBot(settings.TELEGRAM_BOT_TOKEN)
