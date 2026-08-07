from unittest import mock

from celery.exceptions import OperationalError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from .bot import get_bot
from .models import TelegramChat
from .notify import MODERATION, notify
from .tasks import send_message

# Шаблон держим прямо здесь: настоящие появятся вместе с модерацией.
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": False,
    "OPTIONS": {"loaders": [(
        "django.template.loaders.locmem.Loader", {"tg.txt": "Книга <b>{{ title }}</b>\n"},
    )]},
}]


class BotClientTests(SimpleTestCase):
    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_without_a_token_there_is_no_bot(self):
        # Так живёт разработка: телеграм выключен, сайт работает.
        self.assertIsNone(get_bot())

    @override_settings(TELEGRAM_BOT_TOKEN="123:abc", PROXY="http://proxy.local:3128")
    def test_proxy_is_applied_to_the_client(self):
        from telebot import apihelper

        self.addCleanup(setattr, apihelper, "proxy", None)  # настройка модульная, за собой убираем
        self.assertIsNotNone(get_bot())
        self.assertEqual(apihelper.proxy, {"https": "http://proxy.local:3128"})


class BotCommandTests(SimpleTestCase):
    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_command_says_what_is_missing(self):
        with self.assertRaises(CommandError):
            call_command("bot")


@override_settings(TEMPLATES=TEMPLATES)
class NotifyTests(SimpleTestCase):
    def test_text_is_rendered_before_it_goes_into_the_queue(self):
        with mock.patch("telegram.notify.send_message") as task:
            notify(MODERATION, "tg.txt", {"title": "Зорич"})

        task.delay.assert_called_once_with("moderation", "Книга <b>Зорич</b>")

    def test_dangerous_characters_are_escaped(self):
        # parse_mode=HTML, поэтому «<» из названия сломал бы разметку сообщения.
        with mock.patch("telegram.notify.send_message") as task:
            notify(MODERATION, "tg.txt", {"title": "<b>жирно</b>"})

        self.assertIn("&lt;b&gt;", task.delay.call_args.args[1])

    def test_dead_broker_does_not_break_the_caller(self):
        # Уведомление — не потеря: всё то же есть на сайте. Ронять запрос из-за него нельзя.
        with mock.patch("telegram.notify.send_message") as task:
            task.delay.side_effect = OperationalError("брокер недоступен")
            with self.assertLogs("telegram.notify", "ERROR"):  # молча терять всё же не должны
                notify(MODERATION, "tg.txt", {"title": "Зорич"})


class ConsoleTests(TestCase):
    @override_settings(TELEGRAM_CONSOLE=True)
    def test_console_mode_prints_instead_of_sending(self):
        # Так живёт разработка: чат настраивать не надо, сообщение видно в окне воркера.
        TelegramChat.objects.create(name=MODERATION, chat_id=-1001234567890)
        bot = mock.MagicMock()
        with mock.patch("telegram.tasks.get_bot", return_value=bot), \
             mock.patch("telegram.tasks.sys.stdout") as out:
            send_message(MODERATION, "Привет")

        bot.send_message.assert_not_called()
        self.assertIn("Привет", "".join(call.args[0] for call in out.write.call_args_list))


@override_settings(TELEGRAM_CONSOLE=False)
class SendMessageTests(TestCase):
    def send(self, bot):
        with mock.patch("telegram.tasks.get_bot", return_value=bot):
            send_message(MODERATION, "Привет")

    def test_message_goes_to_the_configured_chat_and_topic(self):
        TelegramChat.objects.create(name=MODERATION, chat_id=-1001234567890, topic_id=7)
        bot = mock.MagicMock()

        self.send(bot)

        kwargs = bot.send_message.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], -1001234567890)
        self.assertEqual(kwargs["message_thread_id"], 7)
        self.assertEqual(kwargs["text"], "Привет")
        self.assertEqual(kwargs["parse_mode"], "HTML")

    def test_unconfigured_chat_is_skipped_quietly(self):
        bot = mock.MagicMock()

        self.send(bot)

        bot.send_message.assert_not_called()

    def test_missing_bot_is_skipped_quietly(self):
        TelegramChat.objects.create(name=MODERATION, chat_id=-1001234567890)

        self.send(None)  # падения быть не должно — это и проверяем
