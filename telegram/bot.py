"""Клиент telebot, лениво. Без токена (разработка) бота нет — всё через него молча выключено."""

import telebot
from django.conf import settings
from telebot import apihelper


def get_bot():
    """Клиент или None, если бот не настроен."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return None
    # api.telegram.org заблокирован в РФ — через прокси. Настройка модульная: до первого запроса.
    apihelper.proxy = {"https": settings.PROXY} if settings.PROXY else None
    return telebot.TeleBot(settings.TELEGRAM_BOT_TOKEN)
