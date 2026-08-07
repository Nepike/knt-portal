from django.core.management.base import BaseCommand, CommandError

from telegram.bot import get_bot


class Command(BaseCommand):
    help = "Телеграм-бот: длинный опрос через telebot."

    def handle(self, *args, **options):
        bot = get_bot()
        if not bot:
            raise CommandError("TELEGRAM_BOT_TOKEN не задан")

        @bot.message_handler(commands=["get_chat_id"])
        def get_chat_id(message):
            """Узнать, что вписать в TelegramChat: в группах команда приходит как
            /get_chat_id@имя_бота — разбирать это telebot умеет сам."""
            answer = f"chat_id: {message.chat.id}"
            if message.message_thread_id:
                answer += f"\ntopic_id: {message.message_thread_id}"
            bot.reply_to(message, answer)

        self.stdout.write(self.style.SUCCESS("Бот запущен"))
        # infinity_polling сам держит offset и переживает обрывы сети — ради этого
        # библиотека и взята вместо ручных запросов к getUpdates.
        bot.infinity_polling()
