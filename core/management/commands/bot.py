import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Телеграм-бот: long polling через getUpdates"

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN не задан")
        self.url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/"
        # api.telegram.org заблокирован в РФ — с сервера ходим через прокси
        self.proxies = {"https": settings.PROXY} if settings.PROXY else None

        self.stdout.write("Бот запущен")
        offset = None
        while True:
            try:
                updates = self.call("getUpdates", offset=offset, timeout=30)
            except requests.RequestException as e:
                self.stderr.write(f"Сеть: {e}")
                time.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self.on_update(update)

    def call(self, method, **params):
        # timeout запроса должен превышать timeout long polling, иначе оборвём его сами
        response = requests.get(self.url + method, params=params, proxies=self.proxies, timeout=60)
        response.raise_for_status()
        return response.json()["result"]

    def on_update(self, update):
        message = update.get("message")
        if not message:
            return
        # /get_chat_id@bot_name в группах
        if (message.get("text") or "").split("@")[0] != "/get_chat_id":
            return

        thread_id = message.get("message_thread_id")
        text = f"chat_id: {message['chat']['id']}"
        if thread_id:
            text += f"\ntopic_id: {thread_id}"
        self.call("sendMessage", chat_id=message["chat"]["id"], message_thread_id=thread_id, text=text)
