import re
from base64 import b64decode, b64encode
from unittest import mock

from celery.exceptions import OperationalError
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .bot import get_bot
from .models import TelegramChat
from .notify import ADMIN, MODERATION, notify
from .tasks import CAPTION_LIMIT, CUT, MESSAGE_LIMIT, fit, send_document, send_message, send_photo
from .views import MAX_ARCHIVE

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

    def test_a_picture_goes_into_the_task_itself(self):
        # Воркер — отдельный контейнер: файла на диске веб-процесса он не увидит.
        image = SimpleUploadedFile("доска.png", b"\x89PNG-bytes", content_type="image/png")
        with mock.patch("telegram.notify.send_photo") as task:
            notify(MODERATION, "tg.txt", {"title": "Зорич"}, image=image)

        chat, text, encoded, name = task.delay.call_args.args
        self.assertEqual(b64decode(encoded), b"\x89PNG-bytes")
        self.assertEqual(name, "доска.png")
        self.assertEqual(text, "Книга <b>Зорич</b>")

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


@override_settings(TELEGRAM_CONSOLE=False)
class SendPhotoTests(TestCase):
    def send(self, text):
        TelegramChat.objects.create(name=MODERATION, chat_id=-1001234567890, topic_id=7)
        bot = mock.MagicMock()
        with mock.patch("telegram.tasks.get_bot", return_value=bot):
            send_photo(MODERATION, text, b64encode(b"png-bytes").decode(), "доска.png")
        return bot

    def test_short_text_becomes_the_caption(self):
        bot = self.send("Коротко")

        bot.send_message.assert_not_called()
        kwargs = bot.send_photo.call_args.kwargs
        self.assertEqual(kwargs["caption"], "Коротко")
        self.assertEqual(kwargs["photo"], b"png-bytes")
        self.assertEqual(kwargs["message_thread_id"], 7)

    def test_a_long_report_goes_as_its_own_message_and_the_picture_follows(self):
        # Обрезанное обращение хуже разорванного на два сообщения.
        bot = self.send("а" * (CAPTION_LIMIT + 1))

        bot.send_message.assert_called_once()
        self.assertIsNone(bot.send_photo.call_args.kwargs["caption"])


@override_settings(TELEGRAM_CONSOLE=False)
class SendDocumentTests(TestCase):
    def test_the_file_goes_as_is_under_its_own_name(self):
        TelegramChat.objects.create(name=ADMIN, chat_id=-1001234567890, topic_id=3)
        bot = mock.MagicMock()
        with mock.patch("telegram.tasks.get_bot", return_value=bot):
            send_document(ADMIN, "📦 Аналитика", b64encode(b"PK\x03\x04-bytes").decode(), "stats.zip")

        bot.send_message.assert_not_called()
        kwargs = bot.send_document.call_args.kwargs
        self.assertEqual(kwargs["document"], b"PK\x03\x04-bytes")
        self.assertEqual(kwargs["visible_file_name"], "stats.zip")
        self.assertEqual(kwargs["caption"], "📦 Аналитика")
        self.assertEqual(kwargs["message_thread_id"], 3)

    @override_settings(TELEGRAM_CONSOLE=True)
    def test_on_development_it_is_printed_with_the_file_name(self):
        with mock.patch("telegram.tasks.sys.stdout") as out:
            send_document(ADMIN, "📦 Аналитика", b64encode(b"PK").decode(), "stats.zip")

        self.assertIn("[файл stats.zip]", "".join(call.args[0] for call in out.write.call_args_list))


ZIP = b"PK\x03\x04" + b"\0" * 100
TOKEN = "analytics-token"


@override_settings(ANALYTICS_TOKEN=TOKEN)
class AnalyticsInboxTests(TestCase):
    """Второй сервер шлёт архив скриптом, сайт пересылает его в чат admin и нигде не хранит."""

    def setUp(self):
        cache.clear()  # ограничитель живёт в кэше и переживает тесты
        self.broker = self.patch("telegram.views.celery_app.connection_for_write")
        self.sent = self.patch("telegram.views.send_document.apply_async")

    def patch(self, target):
        patcher = mock.patch(target)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def post(self, content=ZIP, name="stats.zip", token=TOKEN, **data):
        if content is not None:
            data["file"] = SimpleUploadedFile(name, content, content_type="application/octet-stream")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post(reverse("analytics_inbox"), data, headers=headers)

    def test_an_archive_goes_to_the_admin_chat_as_it_came(self):
        response = self.post(note="за неделю <b>")

        self.assertEqual(response.json(), {"ok": True, "name": "stats.zip", "size": len(ZIP)})
        chat, text, encoded, name = self.sent.call_args.kwargs["args"]
        self.assertEqual(chat, ADMIN)
        self.assertEqual(b64decode(encoded), ZIP)
        self.assertEqual(name, "stats.zip")
        self.assertIn("stats.zip", text)
        self.assertIn("за неделю &lt;b&gt;", text)  # подпись идёт в режиме HTML

    def test_without_the_right_token_nothing_is_taken(self):
        for token in ("", "чужой", TOKEN + "x"):
            with self.subTest(token=token):
                self.assertEqual(self.post(token=token).status_code, 401)
        self.sent.assert_not_called()

    @override_settings(ANALYTICS_TOKEN="")
    def test_a_forgotten_token_closes_the_box_instead_of_opening_it(self):
        response = self.post(token="")

        self.assertEqual(response.status_code, 503)
        self.assertIn("ANALYTICS_TOKEN", response.json()["error"])
        self.sent.assert_not_called()

    def test_every_kind_of_archive_is_recognised_by_its_first_bytes(self):
        heads = {
            "zip": ZIP, "пустой zip": b"PK\x05\x06" + b"\0" * 18, "tar.gz": b"\x1f\x8b\x08" + b"\0" * 20,
            "bz2": b"BZh91AY", "xz": b"\xfd7zXZ\x00\0\0", "7z": b"7z\xbc\xaf\x27\x1c\0\4",
            "zst": b"\x28\xb5\x2f\xfd\0", "tar": b"\0" * 257 + b"ustar\x0000" + b"\0" * 50,
        }
        for kind, content in heads.items():
            with self.subTest(kind=kind):
                self.assertEqual(self.post(content, name="stats.bin").status_code, 200)

    def test_anything_else_is_turned_away_whatever_it_is_called(self):
        for content in (b"<html><script>", b"MZ\x90\x00", b"id,visits\n1,2\n"):
            with self.subTest(content=content):
                response = self.post(content, name="stats.zip")
                self.assertEqual(response.status_code, 400)
                self.assertIn("не архив", response.json()["error"])
        self.sent.assert_not_called()

    def test_without_a_file_the_script_is_told_what_to_send(self):
        response = self.post(content=None)

        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.json()["error"])

    def test_a_body_declared_too_big_is_refused_before_it_is_read(self):
        with mock.patch("django.http.request.MultiPartParser") as parser:
            response = self.client.post(
                reverse("analytics_inbox"), {"file": SimpleUploadedFile("stats.zip", ZIP)},
                headers={"Authorization": f"Bearer {TOKEN}"}, CONTENT_LENGTH=str(MAX_ARCHIVE * 2),
            )

        self.assertEqual(response.status_code, 413)
        parser.assert_not_called()

    def test_a_file_over_the_limit_is_refused_even_when_the_length_was_not_declared(self):
        with mock.patch("telegram.views.MAX_ARCHIVE", len(ZIP) - 1):
            response = self.post()

        self.assertEqual(response.status_code, 413)
        self.sent.assert_not_called()

    def test_a_runaway_script_is_stopped_but_others_could_not_spend_its_limit(self):
        with mock.patch("telegram.views.PER_HOUR", 3):
            for _ in range(5):
                self.post(token="чужой")  # без токена — мимо счёта
            answers = [self.post().status_code for _ in range(4)]

        self.assertEqual(answers, [200, 200, 200, 429])
        self.assertEqual(self.sent.call_count, 3)

    def test_with_the_broker_down_the_script_hears_it_at_once(self):
        self.broker.return_value.__enter__.return_value.ensure_connection.side_effect = OSError("нет Redis")
        with self.assertLogs("telegram.views", "WARNING"):
            response = self.post()

        self.assertEqual(response.status_code, 503)
        self.sent.assert_not_called()

    def test_it_is_for_scripts_only(self):
        self.assertEqual(self.client.get(reverse("analytics_inbox")).status_code, 405)


class FitTests(SimpleTestCase):
    """Сообщение длиннее предела телеграм не принимает вовсе — такое уведомление терялось
    бы целиком. А оборванный или незакрытый тег в режиме HTML он не принимает тоже."""

    HEAD = '📁 <b>Новый материал</b>\n<a href="https://knt-mipt.ru/materials/7/">Механика</a>\n'

    def balanced(self, text):
        return all(
            len(re.findall(rf"<{tag}[ >]", text)) == text.count(f"</{tag}>") for tag in ("b", "a", "blockquote")
        )

    def test_a_message_that_fits_is_left_alone(self):
        self.assertEqual(fit(self.HEAD), self.HEAD)
        self.assertEqual(fit("а" * MESSAGE_LIMIT), "а" * MESSAGE_LIMIT)

    def test_a_long_one_is_cut_to_the_limit_and_says_so(self):
        cut = fit(self.HEAD + "строка описания\n" * 500)

        self.assertLessEqual(len(cut), MESSAGE_LIMIT)
        self.assertGreater(len(cut), MESSAGE_LIMIT - 200)  # режем конец, а не всё подряд
        self.assertTrue(cut.startswith(self.HEAD))
        self.assertTrue(cut.endswith("строка описания\n… обрезано"), cut[-60:])

    def test_a_quote_left_open_by_the_cut_is_closed(self):
        cut = fit(self.HEAD + "<blockquote>" + "строка описания\n" * 500 + "</blockquote>\n<b>Год:</b> 2025")

        self.assertTrue(cut.endswith("</blockquote>\n… обрезано"), cut[-60:])
        self.assertTrue(self.balanced(cut))

    def test_one_endless_line_is_cut_in_the_middle_and_not_thrown_away(self):
        """Описание без единого переноса: граница строки тут — начало самого описания."""
        cut = fit(self.HEAD + "<blockquote>" + "а" * 9000 + "</blockquote>")

        self.assertGreater(cut.count("а"), 3500)
        self.assertLessEqual(len(cut), MESSAGE_LIMIT)
        self.assertTrue(self.balanced(cut))

    def test_the_cut_never_lands_inside_a_tag_or_an_escaped_sign(self):
        link = '<a href="https://knt-mipt.ru/materials/7/">ссылка</a>'
        for tail in (link, "&amp;", "&#x27;", "<b>жирно</b>"):
            # Двигаем хвост по одному знаку через место обреза — и ни разу не рвём его.
            for shift in range(len(tail) + 2):
                cut = fit("а" * (MESSAGE_LIMIT - len(CUT) - 32 - shift) + tail + "я" * 600)
                body = cut.removesuffix(CUT)
                with self.subTest(tail=tail, shift=shift):
                    self.assertNotRegex(body, r"<[^>]*$")
                    self.assertNotRegex(body, r"&[#\w]*$")
                    self.assertTrue(self.balanced(cut))


@override_settings(TELEGRAM_CONSOLE=False)
class LongMessageTests(TestCase):
    def setUp(self):
        TelegramChat.objects.create(name=MODERATION, chat_id=-1001234567890)
        self.bot = mock.MagicMock()
        patch = mock.patch("telegram.tasks.get_bot", return_value=self.bot)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_message_over_the_limit_still_goes_out(self):
        send_message(MODERATION, "<blockquote>" + "описание " * 1000 + "</blockquote>")

        sent = self.bot.send_message.call_args.kwargs["text"]
        self.assertLessEqual(len(sent), MESSAGE_LIMIT)
        self.assertTrue(sent.endswith("</blockquote>\n… обрезано"))

    def test_a_long_report_with_a_picture_is_cut_the_same_way(self):
        send_photo(MODERATION, "обращение " * 1000, b64encode(b"png-bytes").decode(), "доска.png")

        self.assertLessEqual(len(self.bot.send_message.call_args.kwargs["text"]), MESSAGE_LIMIT)
        self.bot.send_photo.assert_called_once()
