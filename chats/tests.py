from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image as PilImage

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import QuerySet
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from attachments.models import File, Image
from core.models import Team
from users.models import User, UserSession

from .consumers import ChatConsumer
from .events import chat_group, notify_chat, user_group
from .forms import CuratorAddForm
from .models import Chat, Membership, Message, Reaction, unread_total
from .uploads import MAX_FILES
from .views import ACT_LIMIT, CATCH_UP, MAX_TEXT, PAGE_SIZE, SEND_LIMIT, _chat_items


def make_user(email, name="Иван", surname="Иванов", **extra):
    extra.setdefault("must_change_password", False)  # иначе middleware уведёт на смену пароля
    return User.objects.create_user(email=email, name=name, surname=surname, password="pass12345", **extra)


def make_team(number, stage="bachelor", year=2024):
    return Team.objects.create(
        number=number, profile="Прикладная математика", course_code="03.03.01",
        stage=stage, year_of_admission=year,
    )


class UserSearchTests(TestCase):
    """Строка «с кем начать чат». Правила поиска общие — core/search.py."""

    @classmethod
    def setUpTestData(cls):
        cls.me = make_user("me@t.local")
        cls.maxim = make_user("m@t.local", name="Максим", surname="Щучкин")
        cls.katya = make_user("k@t.local", name="Екатерина", surname="Бажанова", patronymic="Максимовна")

    def setUp(self):
        self.client.force_login(self.me)

    def found(self, q):
        response = self.client.get(reverse("user_search"), {"q": q}, headers={"HX-Request": "true"})
        return response.content.decode()

    def test_full_name_finds_the_person(self):
        # Раньше искали строкой целиком по каждому полю отдельно — и не находили никого.
        for query in ("Максим Щучкин", "Щучкин Максим", "макс щуч"):
            with self.subTest(query=query):
                self.assertIn("Щучкин", self.found(query))

    def test_a_stranger_is_not_dragged_in_by_her_patronymic(self):
        page = self.found("Максим")
        self.assertIn("Щучкин", page)
        self.assertNotIn("Бажанова", page)

    def test_i_am_never_among_the_found(self):
        self.assertNotIn("Никого не нашлось", self.found("Максим"))
        self.assertIn("Никого не нашлось", self.found("Иванов"))


class DirectChatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local", "Алиса", "Аброва")
        cls.bob = make_user("b@t.local", "Борис", "Бобров")

    def test_dm_is_unique_for_a_pair(self):
        first = Chat.get_or_create_dm(self.alice, self.bob)
        second = Chat.get_or_create_dm(self.bob, self.alice)  # порядок не важен
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Membership.objects.filter(chat=first).count(), 2)

    def test_dm_start_redirects_to_chat(self):
        self.client.force_login(self.alice)
        response = self.client.post(reverse("dm_start", args=[self.bob.pk]))
        chat = Chat.objects.get(kind="dm")
        self.assertRedirects(response, reverse("chat_detail", args=[chat.pk]))

    def test_dm_start_with_self_goes_back_to_list(self):
        self.client.force_login(self.alice)
        response = self.client.post(reverse("dm_start", args=[self.alice.pk]))
        self.assertRedirects(response, reverse("chat_list"))
        self.assertFalse(Chat.objects.exists())

    def test_a_link_alone_does_not_create_a_dialogue(self):
        """Открытие диалога пишет в базу, а по ссылке за человека ходят предзагрузчики."""
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse("dm_start", args=[self.bob.pk])).status_code, 405)
        self.assertFalse(Chat.objects.exists())

    def test_empty_dm_hidden_from_list(self):
        Chat.get_or_create_dm(self.alice, self.bob)
        self.client.force_login(self.alice)
        response = self.client.get(reverse("chat_list"))
        self.assertContains(response, "Чатов пока нет")

    def test_other_member_resolves_partner(self):
        chat = Chat.get_or_create_dm(self.alice, self.bob)
        self.assertEqual(chat.other_member(self.alice), self.bob)
        self.assertEqual(chat.other_member(self.bob), self.alice)


class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.stranger = make_user("s@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)
        cls.message = Message.objects.create(chat=cls.chat, author=cls.alice, text="привет")

    def setUp(self):
        self.client.force_login(self.stranger)

    def test_stranger_cannot_open_chat(self):
        self.assertEqual(self.client.get(reverse("chat_detail", args=[self.chat.pk])).status_code, 404)

    def test_stranger_cannot_poll(self):
        self.assertEqual(self.client.get(reverse("messages_new", args=[self.chat.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("messages_older", args=[self.chat.pk])).status_code, 404)

    def test_stranger_cannot_send(self):
        response = self.client.post(reverse("message_send", args=[self.chat.pk]), {"text": "вторжение"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.chat.messages.count(), 1)

    def test_stranger_cannot_touch_message(self):
        self.assertEqual(self.client.get(reverse("message_card", args=[self.message.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("message_delete", args=[self.message.pk])).status_code, 404)
        self.assertEqual(
            self.client.post(reverse("message_react", args=[self.message.pk]), {"emoji": "🔥"}).status_code, 404
        )

    def test_anonymous_is_redirected(self):
        self.client.logout()
        response = self.client.get(reverse("chat_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


class UnreadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def test_counts_only_foreign_messages(self):
        Message.objects.create(chat=self.chat, author=self.bob, text="раз")
        Message.objects.create(chat=self.chat, author=self.bob, text="два")
        Message.objects.create(chat=self.chat, author=self.alice, text="своё не считается")
        self.assertEqual(unread_total(self.alice), 2)
        self.assertEqual(unread_total(self.bob), 1)

    def test_deleted_messages_are_not_counted(self):
        message = Message.objects.create(chat=self.chat, author=self.bob, text="ой")
        message.deleted = True
        message.save(update_fields=["deleted"])
        self.assertEqual(unread_total(self.alice), 0)

    def test_opening_chat_marks_read(self):
        Message.objects.create(chat=self.chat, author=self.bob, text="привет")
        self.client.force_login(self.alice)
        self.client.get(reverse("chat_detail", args=[self.chat.pk]))
        self.assertEqual(unread_total(self.alice), 0)

    def test_badge_view_reports_count(self):
        Message.objects.create(chat=self.chat, author=self.bob, text="привет")
        self.client.force_login(self.alice)
        response = self.client.get(reverse("unread_badge"), headers={"HX-Request": "true"})
        self.assertContains(response, ">1</span>")


class HistoryPaginationTests(TestCase):
    """Открываем последнюю страницу, остальное подтягиваем маячком сверху.

    Всё прочитано намеренно: с непрочитанными чат открывается с них (см. UnreadOpenTests),
    а здесь проверяется сама пагинация.
    """

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)
        cls.messages = [
            Message.objects.create(chat=cls.chat, author=cls.bob, text=f"сообщение {i}") for i in range(75)
        ]

    def setUp(self):
        self.client.force_login(self.alice)
        Membership.objects.filter(chat=self.chat, user=self.alice).update(last_read=self.messages[-1])

    def test_page_shows_last_30_with_sentinel(self):
        response = self.client.get(reverse("chat_detail", args=[self.chat.pk]))
        self.assertEqual(response.content.decode().count('data-id="'), 30)
        self.assertContains(response, 'id="history-top"')
        self.assertContains(response, "сообщение 74")
        self.assertNotContains(response, "сообщение 44")
        self.assertContains(response, f"before={self.messages[45].pk}")  # курсор = первое показанное

    def test_paging_reaches_the_beginning(self):
        url = reverse("messages_older", args=[self.chat.pk])
        response = self.client.get(url, {"before": self.messages[45].pk})
        self.assertEqual(response.content.decode().count('data-id="'), 30)
        self.assertContains(response, 'id="history-top"')

        response = self.client.get(url, {"before": self.messages[15].pk})
        self.assertEqual(response.content.decode().count('data-id="'), 15)
        self.assertContains(response, "сообщение 0")
        self.assertNotContains(response, 'id="history-top"')  # больше нечего грузить

    def test_older_page_does_not_mark_read(self):
        membership = Membership.objects.get(chat=self.chat, user=self.alice)
        membership.last_read = None  # как будто не читали ничего: листание не должно это менять
        membership.save(update_fields=["last_read"])
        self.client.get(reverse("messages_older", args=[self.chat.pk]), {"before": self.messages[45].pk})
        membership.refresh_from_db()
        self.assertIsNone(membership.last_read_id)


class SendTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты
        self.client.force_login(self.alice)
        self.url = reverse("message_send", args=[self.chat.pk])

    def test_send_creates_message_and_updates_preview(self):
        response = self.client.post(self.url, {"text": "  привет  "})
        message = Message.objects.get()
        self.assertEqual(message.text, "привет")  # обрезали пробелы
        self.assertContains(response, "привет")
        self.chat.refresh_from_db()
        self.assertEqual(self.chat.last_message_id, message.pk)

    def test_blank_text_is_ignored(self):
        self.assertEqual(self.client.post(self.url, {"text": "   "}).status_code, 204)
        self.assertFalse(Message.objects.exists())

    def test_text_is_capped_server_side(self):
        self.client.post(self.url, {"text": "я" * 9000})
        self.assertEqual(len(Message.objects.get().text), 4000)

    def test_reply_from_other_chat_is_dropped(self):
        foreign = Chat.get_or_create_dm(self.alice, make_user("c@t.local"))
        alien = Message.objects.create(chat=foreign, author=self.alice, text="чужое")
        self.client.post(self.url, {"text": "ответ", "reply_to": alien.pk})
        self.assertIsNone(Message.objects.get(chat=self.chat).reply_to_id)

    def test_answer_carries_messages_missed_since_last_poll(self):
        """Курсор ленты сдвинется на наш id — чужое сообщение с меньшим id
        обязано приехать в этом же ответе, иначе оно потеряется навсегда."""
        mine = Message.objects.create(chat=self.chat, author=self.alice, text="старое своё")
        Message.objects.create(chat=self.chat, author=self.bob, text="пришло пока я печатал")
        body = self.client.post(self.url, {"text": "моя реплика", "after": mine.pk}).content.decode()
        self.assertIn("пришло пока я печатал", body)
        self.assertIn("моя реплика", body)
        self.assertNotIn("старое своё", body)  # уже в DOM, повторно не шлём
        self.assertLess(body.index("пришло пока я печатал"), body.index("моя реплика"))

    def test_without_cursor_answer_holds_only_own_message(self):
        """Курсор потерян (пустой чат или сломанный JS) — не вываливаем всю историю."""
        Message.objects.create(chat=self.chat, author=self.bob, text="древнее")
        response = self.client.post(self.url, {"text": "моя реплика"})
        self.assertNotContains(response, "древнее")
        self.assertContains(response, "моя реплика")

    def test_a_cursor_from_the_future_still_answers(self):
        """Курсора младше только что созданного сообщения честно не бывает: в ленте у
        отправителя ничего новее нет. Раньше такой запрос отвечал пятисоткой — ответ
        выходил пустым, а пустой ленте нечего искать соседа сверху."""
        response = self.client.post(self.url, {"text": "моя реплика", "after": "999999"})
        self.assertContains(response, "моя реплика")

    def test_send_marks_own_message_read(self):
        self.client.post(self.url, {"text": "привет"})
        membership = Membership.objects.get(chat=self.chat, user=self.alice)
        self.assertEqual(membership.last_read_id, Message.objects.get().pk)


class PollingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)
        cls.message = Message.objects.create(chat=cls.chat, author=cls.bob, text="привет")

    def setUp(self):
        self.client.force_login(self.alice)
        self.url = reverse("messages_new", args=[self.chat.pk])

    def test_new_message_arrives_once(self):
        response = self.client.get(self.url, {"after": 0})
        self.assertContains(response, "привет")
        self.assertNotContains(response, "hx-swap-oob")  # свежее — не «изменённое»
        self.assertNotContains(self.client.get(self.url, {"after": self.message.pk}), "привет")

    def test_edited_message_comes_back_as_oob(self):
        self.message.text = "исправил"
        self.message.updated = timezone.now()
        self.message.save(update_fields=["text", "updated"])
        response = self.client.get(self.url, {"after": self.message.pk, "since": self.message.pk})
        self.assertContains(response, "hx-swap-oob")
        self.assertContains(response, "исправил")

    def test_edit_of_a_message_outside_the_screen_is_not_sent(self):
        """Замену тому, чего у вкладки нет в ленте, htmx выбрасывает с ошибкой в консоль."""
        newer = Message.objects.create(chat=self.chat, author=self.bob, text="второе")
        self.message.text = "исправил"
        self.message.updated = timezone.now()
        self.message.save(update_fields=["text", "updated"])
        # у вкладки в ленте только `newer` — значит и нижний край, и верхний равны ему
        response = self.client.get(self.url, {"after": newer.pk, "since": newer.pk})
        self.assertNotContains(response, "исправил")

    def test_garbage_cursor_does_not_break(self):
        self.assertEqual(self.client.get(self.url, {"after": "абв"}).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("messages_older", args=[self.chat.pk]), {"before": "-"}).status_code, 200
        )
        self.assertEqual(self.client.get(reverse("chat_list_fragment"), {"active": "нет"}).status_code, 200)

    def test_poll_stays_cheap(self):
        """Опрос идёт каждые 3 секунды у каждой вкладки — лишним запросам тут не место."""
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.url, {"after": self.message.pk}, headers={"HX-Request": "true"})
        self.assertLessEqual(len(ctx), 5, [q["sql"][:120] for q in ctx.captured_queries])


class EditDeleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def setUp(self):
        self.message = Message.objects.create(chat=self.chat, author=self.alice, text="исходный")

    def test_author_edits_and_message_is_marked(self):
        self.client.force_login(self.alice)
        response = self.client.post(reverse("message_edit", args=[self.message.pk]), {"text": "новый"})
        self.assertContains(response, "новый")
        self.message.refresh_from_db()
        self.assertEqual(self.message.text, "новый")
        self.assertIsNotNone(self.message.edited)
        self.assertIsNotNone(self.message.updated)  # чужие вкладки узнают через поллинг

    def test_stranger_in_chat_cannot_edit(self):
        self.client.force_login(self.bob)
        self.assertEqual(
            self.client.post(reverse("message_edit", args=[self.message.pk]), {"text": "хак"}).status_code, 403
        )
        self.message.refresh_from_db()
        self.assertEqual(self.message.text, "исходный")

    def test_delete_is_soft(self):
        self.client.force_login(self.alice)
        response = self.client.post(reverse("message_delete", args=[self.message.pk]))
        self.assertContains(response, "сообщение удалено")
        self.message.refresh_from_db()
        self.assertTrue(self.message.deleted)
        self.assertIsNotNone(self.message.updated)

    def test_member_cannot_delete_foreign_message(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.post(reverse("message_delete", args=[self.message.pk])).status_code, 403)

    def test_group_admin_deletes_foreign_message(self):
        chat = Chat.objects.create(kind="group", title="Группа")
        Membership.objects.create(chat=chat, user=self.alice)
        Membership.objects.create(chat=chat, user=self.bob, is_admin=True)
        message = Message.objects.create(chat=chat, author=self.alice, text="нарушение")
        self.client.force_login(self.bob)
        self.assertEqual(self.client.post(reverse("message_delete", args=[message.pk])).status_code, 200)
        message.refresh_from_db()
        self.assertTrue(message.deleted)

    def test_deleted_message_cannot_be_edited(self):
        self.message.deleted = True
        self.message.save(update_fields=["deleted"])
        self.client.force_login(self.alice)
        self.assertEqual(
            self.client.post(reverse("message_edit", args=[self.message.pk]), {"text": "х"}).status_code, 403
        )

    def test_quote_of_deleted_message_is_hidden(self):
        answer = Message.objects.create(chat=self.chat, author=self.bob, text="ответ", reply_to=self.message)
        self.message.deleted = True
        self.message.save(update_fields=["deleted"])
        self.client.force_login(self.bob)
        response = self.client.get(reverse("message_card", args=[answer.pk]))
        self.assertNotContains(response, "исходный")


class ReactionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def setUp(self):
        self.message = Message.objects.create(chat=self.chat, author=self.bob, text="привет")
        self.client.force_login(self.alice)
        self.url = reverse("message_react", args=[self.message.pk])

    def test_reaction_toggles(self):
        self.client.post(self.url, {"emoji": "🔥"})
        self.assertEqual(self.message.reactions.count(), 1)
        self.client.post(self.url, {"emoji": "🔥"})  # повторный клик — снять
        self.assertEqual(self.message.reactions.count(), 0)

    def test_answer_already_contains_the_reaction(self):
        """Пузырь рисуется после записи — иначе реакция «появляется не сразу»."""
        response = self.client.post(self.url, {"emoji": "🔥"})
        self.assertContains(response, "🔥</span>1")

    def test_unknown_emoji_is_ignored(self):
        self.client.post(self.url, {"emoji": "<script>"})
        self.assertEqual(self.message.reactions.count(), 0)

    def test_reaction_marks_message_updated(self):
        self.client.post(self.url, {"emoji": "🔥"})
        self.message.refresh_from_db()
        self.assertIsNotNone(self.message.updated)

    def test_deleted_message_takes_no_reactions(self):
        self.message.deleted = True
        self.message.save(update_fields=["deleted"])
        self.client.post(self.url, {"emoji": "🔥"})
        self.assertEqual(self.message.reactions.count(), 0)

    def test_two_taps_at_once_do_not_break(self):
        """Гонка двух вкладок: обе не нашли реакции и обе побежали её ставить.

        Проверка «есть? — нет, создаём» проходила у обеих, и вторая вставка билась
        об уникальный индекс пятисоткой. Соперника изображаем так: первый поиск
        реакции промахивается, строка при этом уже есть.
        """
        Reaction.objects.create(message=self.message, user=self.alice, emoji="🔥")
        real_get, missed = QuerySet.get, []

        def blind_first_look(self, *args, **kwargs):
            if self.model is Reaction and not missed:
                missed.append(True)
                raise Reaction.DoesNotExist
            return real_get(self, *args, **kwargs)

        with mock.patch.object(QuerySet, "get", blind_first_look):
            response = self.client.post(self.url, {"emoji": "🔥"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(missed)  # подмена сработала, иначе тест ничего не проверил


class GroupManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user("adm@t.local", "Анна", "Админова")
        cls.member = make_user("m@t.local", "Мария", "Мирова")
        cls.outsider = make_user("o@t.local", "Олег", "Орлов")

    def create_group(self):
        chat = Chat.objects.create(kind="group", title="Проект")
        Membership.objects.create(chat=chat, user=self.admin, is_admin=True)
        Membership.objects.create(chat=chat, user=self.member)
        return chat

    def test_creator_becomes_admin(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("chat_create_group"), {"title": "Проект", "members": [self.member.pk]})
        chat = Chat.objects.get(kind="group")
        self.assertRedirects(response, reverse("chat_detail", args=[chat.pk]))
        self.assertTrue(Membership.objects.get(chat=chat, user=self.admin).is_admin)
        self.assertFalse(Membership.objects.get(chat=chat, user=self.member).is_admin)

    def test_invalid_form_reopens_modal(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("chat_create_group"), {"title": "", "members": [self.member.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Chat.objects.exists())

    def test_rename_is_admin_only(self):
        chat = self.create_group()
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(reverse("chat_rename", args=[chat.pk]), {"title": "Моё"}).status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(reverse("chat_rename", args=[chat.pk]), {"title": "Новое имя"})
        chat.refresh_from_db()
        self.assertEqual(chat.title, "Новое имя")
        self.assertIn("Новое имя", chat.last_message.text)  # системная строка в ленте

    def test_admin_adds_members(self):
        chat = self.create_group()
        self.client.force_login(self.admin)
        self.client.post(reverse("chat_add_members", args=[chat.pk]), {"members": [self.outsider.pk]})
        self.assertTrue(Membership.objects.filter(chat=chat, user=self.outsider).exists())
        self.assertIn("Орлов", chat.messages.latest("id").text)

    def test_member_cannot_add(self):
        chat = self.create_group()
        self.client.force_login(self.member)
        self.client.post(reverse("chat_add_members", args=[chat.pk]), {"members": [self.outsider.pk]})
        self.assertFalse(Membership.objects.filter(chat=chat, user=self.outsider).exists())

    def test_admin_removes_member_but_not_self(self):
        chat = self.create_group()
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.post(reverse("chat_remove_member", args=[chat.pk, self.admin.pk])).status_code, 403
        )
        self.client.post(reverse("chat_remove_member", args=[chat.pk, self.member.pk]))
        self.assertFalse(Membership.objects.filter(chat=chat, user=self.member).exists())

    def test_admin_cannot_remove_another_admin(self):
        chat = self.create_group()
        Membership.objects.filter(chat=chat, user=self.member).update(is_admin=True)
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.post(reverse("chat_remove_member", args=[chat.pk, self.member.pk])).status_code, 403
        )
        self.assertTrue(Membership.objects.filter(chat=chat, user=self.member).exists())

    def test_leaving_admin_hands_over(self):
        """Иначе группа осталась бы без управления навсегда."""
        chat = self.create_group()
        Membership.objects.create(chat=chat, user=self.outsider)
        self.client.force_login(self.admin)
        self.client.post(reverse("chat_leave", args=[chat.pk]))
        self.assertTrue(Membership.objects.get(chat=chat, user=self.member).is_admin)
        self.assertFalse(Membership.objects.get(chat=chat, user=self.outsider).is_admin)
        self.assertIn("администратор", chat.messages.latest("id").text)

    def test_last_member_leaving_removes_group(self):
        chat = self.create_group()
        self.client.force_login(self.member)
        self.client.post(reverse("chat_leave", args=[chat.pk]))
        self.assertTrue(Chat.objects.filter(pk=chat.pk).exists())  # админ ещё внутри
        self.client.force_login(self.admin)
        self.client.post(reverse("chat_leave", args=[chat.pk]))
        self.assertFalse(Chat.objects.filter(pk=chat.pk).exists())

    def test_delete_is_admin_only(self):
        chat = self.create_group()
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(reverse("chat_delete", args=[chat.pk])).status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(reverse("chat_delete", args=[chat.pk]))
        self.assertFalse(Chat.objects.filter(pk=chat.pk).exists())

    def test_dm_cannot_be_left(self):
        chat = Chat.get_or_create_dm(self.admin, self.member)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse("chat_leave", args=[chat.pk])).status_code, 403)


class CourseChatTests(TestCase):
    """Чат курса ведёт сигнал: одна лента на весь поток, состав едет за полем team."""

    @classmethod
    def setUpTestData(cls):
        cls.a24 = make_team("Б01-001", year=2024)
        cls.b24 = make_team("Б01-002", year=2024)  # тот же курс, другая группа
        cls.a25 = make_team("Б01-003", year=2025)
        cls.m24 = make_team("М01-001", stage="master", year=2024)

    def curator(self, email="c@t.local"):
        user = make_user(email, "Кира", "Курова")
        user.user_permissions.add(Permission.objects.get(codename="curate_course_chats"))
        return user

    def test_whole_course_lands_in_one_chat(self):
        first = make_user("s1@t.local", team=self.a24)
        second = make_user("s2@t.local", team=self.b24)
        chat = Chat.objects.get(kind="course")
        self.assertEqual(chat.title, "Бакалавриат 2024")
        self.assertEqual(chat.memberships.count(), 2)
        self.assertTrue(Membership.objects.filter(chat=chat, user=first).exists())
        self.assertTrue(Membership.objects.filter(chat=chat, user=second).exists())

    def test_year_and_stage_split_courses(self):
        make_user("s1@t.local", team=self.a24)
        make_user("s2@t.local", team=self.a25)
        make_user("s3@t.local", team=self.m24)  # магистры 2024 — не бакалавры 2024
        self.assertEqual(Chat.objects.filter(kind="course").count(), 3)
        self.assertTrue(Chat.objects.filter(title="Магистратура 2024").exists())

    def test_transfer_inside_the_course_keeps_chat(self):
        student = make_user("s@t.local", team=self.a24)
        chat = Chat.objects.get(kind="course")
        student.team = self.b24
        student.save()
        self.assertEqual(Chat.objects.filter(kind="course").count(), 1)
        self.assertTrue(Membership.objects.filter(chat=chat, user=student).exists())

    def test_transfer_to_another_course_moves_membership(self):
        student = make_user("s@t.local", team=self.a24)
        student.team = self.a25
        student.save()
        self.assertFalse(Membership.objects.filter(user=student, chat__admission_year=2024).exists())
        self.assertTrue(Membership.objects.filter(user=student, chat__admission_year=2025).exists())

    def test_unrelated_save_keeps_membership(self):
        student = make_user("s@t.local", team=self.a24)
        student.phone = "+70000000000"
        student.save(update_fields=["phone"])
        self.assertTrue(Membership.objects.filter(user=student, chat__kind="course").exists())

    def test_own_course_chat_cannot_be_left_or_deleted(self):
        student = make_user("s@t.local", team=self.a24)
        chat = Chat.objects.get(kind="course")
        self.client.force_login(student)
        self.assertEqual(self.client.post(reverse("chat_leave", args=[chat.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("chat_delete", args=[chat.pk])).status_code, 403)

    def test_curator_is_invited_by_any_member_and_moderates(self):
        student = make_user("s@t.local", team=self.a24)
        curator = self.curator()
        chat = Chat.objects.get(kind="course")

        self.client.force_login(student)  # обычный участник, не админ
        self.client.post(reverse("chat_add_members", args=[chat.pk]), {"members": [curator.pk]})
        self.assertTrue(Membership.objects.get(chat=chat, user=curator).is_admin)

        message = Message.objects.create(chat=chat, author=student, text="нарушение")
        self.client.force_login(curator)
        self.assertEqual(self.client.post(reverse("message_delete", args=[message.pk])).status_code, 200)

    def test_curator_may_leave_a_foreign_course(self):
        student = make_user("s@t.local", team=self.a24)
        curator = self.curator()
        chat = Chat.objects.get(kind="course")
        self.client.force_login(student)
        self.client.post(reverse("chat_add_members", args=[chat.pk]), {"members": [curator.pk]})

        self.client.force_login(curator)
        self.client.post(reverse("chat_leave", args=[chat.pk]))
        self.assertFalse(Membership.objects.filter(chat=chat, user=curator).exists())

    def test_plain_user_cannot_be_invited(self):
        student = make_user("s@t.local", team=self.a24)
        other = make_user("o@t.local")
        chat = Chat.objects.get(kind="course")
        self.client.force_login(student)
        self.client.post(reverse("chat_add_members", args=[chat.pk]), {"members": [other.pk]})
        self.assertFalse(Membership.objects.filter(chat=chat, user=other).exists())

    def test_a_namesake_right_of_another_app_is_not_the_curator_right(self):
        """codename уникален только внутри своей модели, и куратора мы ищем по нему."""
        make_user("s@t.local", team=self.a24)  # с ним заводится и сам чат курса
        impostor = make_user("i@t.local")
        impostor.user_permissions.add(Permission.objects.create(
            codename="curate_course_chats", name="Однофамилец",
            content_type=ContentType.objects.get_for_model(User),
        ))
        form = CuratorAddForm(chat=Chat.objects.get(kind="course"))
        self.assertNotIn(impostor, form.fields["members"].queryset)

    def test_curator_keeps_membership_after_profile_save(self):
        make_user("s@t.local", team=self.a24)
        curator = self.curator()
        chat = Chat.objects.get(kind="course")
        Membership.objects.create(chat=chat, user=curator, is_admin=True)

        curator = User.objects.get(pk=curator.pk)  # has_perm кэшируется на инстансе
        curator.save()
        self.assertTrue(Membership.objects.filter(chat=chat, user=curator).exists())


class MessageMenuTests(TestCase):
    """Меню сообщения собирается из data-* пузыря: набор атрибутов = набор прав."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)
        cls.mine = Message.objects.create(chat=cls.chat, author=cls.alice, text="моё")

    def card(self, message):
        return self.client.get(reverse("message_card", args=[message.pk]))

    def test_own_message_offers_edit_and_delete(self):
        self.client.force_login(self.alice)
        response = self.card(self.mine)
        self.assertContains(response, f'data-msg="{self.mine.pk}"')
        self.assertContains(response, reverse("message_edit", args=[self.mine.pk]))
        self.assertContains(response, reverse("message_delete", args=[self.mine.pk]))

    def test_foreign_message_offers_neither(self):
        self.client.force_login(self.bob)
        response = self.card(self.mine)
        self.assertContains(response, reverse("message_react", args=[self.mine.pk]))  # реакция доступна всем
        self.assertNotContains(response, reverse("message_edit", args=[self.mine.pk]))
        self.assertNotContains(response, reverse("message_delete", args=[self.mine.pk]))

    def test_chat_admin_gets_delete_on_foreign_message(self):
        chat = Chat.objects.create(kind="group", title="Группа")
        Membership.objects.create(chat=chat, user=self.alice)
        Membership.objects.create(chat=chat, user=self.bob, is_admin=True)
        message = Message.objects.create(chat=chat, author=self.alice, text="нарушение")
        self.client.force_login(self.bob)
        response = self.card(message)
        self.assertContains(response, reverse("message_delete", args=[message.pk]))
        self.assertNotContains(response, reverse("message_edit", args=[message.pk]))

    def test_deleted_message_has_no_menu(self):
        self.mine.deleted = True
        self.mine.save(update_fields=["deleted"])
        self.client.force_login(self.alice)
        self.assertNotContains(self.card(self.mine), "data-msg=")

    def test_system_message_has_no_menu(self):
        system = Message.objects.create(chat=self.chat, text="Кто-то покинул группу")
        self.client.force_login(self.alice)
        self.assertNotContains(self.card(system), "data-msg=")


class PublishTests(TestCase):
    """Кто и о чём сообщает в шину. Транспорт проверен в ConsumerTests, здесь — проводка:
    забытый вызов означает, что у собеседника просто ничего не появится."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.carol = make_user("c@t.local")
        cls.dm = Chat.get_or_create_dm(cls.alice, cls.bob)
        cls.message = Message.objects.create(chat=cls.dm, author=cls.alice, text="привет")

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты
        self.client.force_login(self.alice)

    def group(self):
        chat = Chat.objects.create(kind="group", title="Проект")
        Membership.objects.create(chat=chat, user=self.alice, is_admin=True)
        Membership.objects.create(chat=chat, user=self.bob)
        return chat

    def test_send_notifies_chat_with_the_message(self):
        """С сообщением: по нему получатель поправит счётчик, не спрашивая сервер."""
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("message_send", args=[self.dm.pk]), {"text": "ещё"})
        notify.assert_called_once_with(self.dm.pk, Message.objects.get(text="ещё"))

    def test_reaction_and_delete_notify_chat(self):
        """С видом изменения: по нему вкладка решает, что перечитывать. Реакция не
        трогает ни счётчик, ни список чатов, а удаление — и то и другое."""
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("message_react", args=[self.message.pk]), {"emoji": "🔥"})
            self.client.post(reverse("message_delete", args=[self.message.pk]))
        self.assertEqual(
            notify.call_args_list,
            [mock.call(self.dm.pk, kind="react"), mock.call(self.dm.pk, kind="delete")],
        )

    def test_a_system_line_notifies_with_its_message(self):
        """Она такое же новое сообщение. Без него получатели молча вернулись бы к запросу
        за счётчиком — ровно к тому, от чего событие и обросло данными."""
        chat = self.group()
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("chat_rename", args=[chat.pk]), {"title": "Новое имя"})
        notify.assert_called_once_with(chat.pk, Message.objects.get(chat=chat, author=None))

    def test_edit_notifies_chat(self):
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("message_edit", args=[self.message.pk]), {"text": "правка"})
        notify.assert_called_once_with(self.dm.pk, kind="edit")

    def test_edit_without_changes_stays_quiet(self):
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("message_edit", args=[self.message.pk]), {"text": self.message.text})
        notify.assert_not_called()

    def test_new_dm_notifies_the_other_side(self):
        """Собеседник не подписан на группу чата, которого секунду назад не было."""
        with mock.patch("chats.views.notify_joined") as notify:
            self.client.post(reverse("dm_start", args=[self.carol.pk]))
        chat = Chat.objects.get(kind="dm", dm_key=Chat.dm_key_for(self.alice, self.carol))
        notify.assert_called_once_with(self.carol.pk, chat.pk)

    def test_group_creation_notifies_members(self):
        with mock.patch("chats.views.notify_joined") as notify:
            self.client.post(reverse("chat_create_group"), {"title": "Проект", "members": [self.bob.pk]})
        chat = Chat.objects.get(kind="group")
        notify.assert_called_once_with(self.bob.pk, chat.pk)

    def test_adding_member_notifies_him(self):
        chat = self.group()
        with mock.patch("chats.views.notify_joined") as notify:
            self.client.post(reverse("chat_add_members", args=[chat.pk]), {"members": [self.carol.pk]})
        notify.assert_called_once_with(self.carol.pk, chat.pk)

    def test_removed_member_is_unsubscribed(self):
        chat = self.group()
        with mock.patch("chats.views.notify_left") as notify:
            self.client.post(reverse("chat_remove_member", args=[chat.pk, self.bob.pk]))
        notify.assert_called_once_with(self.bob.pk, chat.pk)

    def test_leaving_unsubscribes_self(self):
        chat = self.group()
        with mock.patch("chats.views.notify_left") as notify:
            self.client.post(reverse("chat_leave", args=[chat.pk]))
        notify.assert_called_once_with(self.alice.pk, chat.pk)

    def test_delete_unsubscribes_every_member(self):
        """Каждому лично, а не в группу чата: от общего события сокет остался бы в группе
        несуществующего чата и слушал бы её до переподключения."""
        chat = self.group()
        with mock.patch("chats.views.notify_left") as notify:
            self.client.post(reverse("chat_delete", args=[chat.pk]))
        self.assertEqual(
            sorted(call.args for call in notify.call_args_list),
            sorted([(self.alice.pk, chat.pk), (self.bob.pk, chat.pk)]),
        )
        self.assertFalse(Chat.objects.filter(pk=chat.pk).exists())


class EventPayloadTests(TestCase):
    """Что именно уходит в сокет. От этого зависит, пойдёт ли вкладка на сервер: раньше
    она ходила за счётчиком непрочитанных на КАЖДОЕ сообщение в каждом своём чате, и в
    чате курса одно сообщение стоило столько запросов, сколько там людей онлайн."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def published(self, *args):
        with mock.patch("chats.events._publish") as publish:
            notify_chat(*args)
        return publish.call_args.args[1]

    def test_a_new_message_says_whose_it_is_and_which(self):
        message = Message.objects.create(chat=self.chat, author=self.bob, text="привет")
        self.assertEqual(
            self.published(self.chat.pk, message),
            {"type": "chat.event", "chat": self.chat.pk, "msg": message.pk, "author": self.bob.pk},
        )

    def test_a_system_line_has_no_author_but_still_counts(self):
        """Пустой автор на клиенте сошёлся бы за «моё», и строка не попала бы в счётчик."""
        message = Message.objects.create(chat=self.chat, text="Кто-то покинул группу")
        self.assertEqual(self.published(self.chat.pk, message)["author"], 0)

    def test_a_change_says_only_which_chat(self):
        """Правку и удаление на клиенте не сосчитать — за числом пусть идёт к серверу."""
        self.assertEqual(self.published(self.chat.pk), {"type": "chat.event", "chat": self.chat.pk})

    def test_a_change_says_what_exactly_changed(self):
        """По виду изменения вкладка решает, что перечитывать. Без него реакция в чате
        курса стоила бы трёх запросов с каждой открытой вкладки — за счётчиком, за
        списком чатов и за лентой, — хотя меняется в ней один пузырь."""
        with mock.patch("chats.events._publish") as publish:
            notify_chat(self.chat.pk, kind="react")
        self.assertEqual(publish.call_args.args[1]["kind"], "react")



class ChannelLayerConfigTests(TestCase):
    def test_socket_timeout_outlives_the_blocking_read(self):
        """Совпадение этих двух чисел рвёт живые сокеты раз в 5 секунд тишины:
        redis-py считает чтение зависшим ровно тогда, когда channels_redis его ещё ждёт."""
        from channels_redis.core import RedisChannelLayer

        socket_timeout = settings.REDIS_HOSTS[0]["socket_timeout"]
        self.assertGreater(socket_timeout, RedisChannelLayer.brpop_timeout)


class ConsumerTests(TransactionTestCase):
    """Сокет чата. TransactionTestCase, а не TestCase: консьюмер ходит в БД из другого
    потока и данных незакоммиченной транзакции теста просто не увидел бы."""

    def setUp(self):
        self.alice = make_user("a@t.local")
        self.bob = make_user("b@t.local")
        self.stranger = make_user("s@t.local")
        self.chat = Chat.get_or_create_dm(self.alice, self.bob)

    async def open_socket(self, user):
        socket = WebsocketCommunicator(ChatConsumer.as_asgi(), "/ws/chats/")
        socket.scope["user"] = user  # обычно ставит AuthMiddlewareStack по cookie сессии
        connected, _ = await socket.connect()
        return socket, connected

    async def test_anonymous_is_refused(self):
        socket, connected = await self.open_socket(AnonymousUser())
        self.assertFalse(connected)
        await socket.disconnect()

    async def test_member_gets_event_of_his_chat(self):
        socket, connected = await self.open_socket(self.alice)
        self.assertTrue(connected)
        await get_channel_layer().group_send(chat_group(self.chat.pk), {"type": "chat.event", "chat": self.chat.pk})
        self.assertEqual(await socket.receive_json_from(), {"chat": self.chat.pk})
        await socket.disconnect()

    async def test_the_event_reaches_the_browser_whole(self):
        """Счётчик на клиенте считается по author и msg. Срежь их консьюмер — вкладка
        молча вернулась бы к запросу на сервер за каждым сообщением."""
        socket, _ = await self.open_socket(self.alice)
        await get_channel_layer().group_send(chat_group(self.chat.pk), {
            "type": "chat.event", "chat": self.chat.pk, "msg": 7, "author": self.bob.pk,
        })
        self.assertEqual(
            await socket.receive_json_from(), {"chat": self.chat.pk, "msg": 7, "author": self.bob.pk}
        )
        await socket.disconnect()

    async def test_the_kind_of_change_reaches_the_browser(self):
        """По нему вкладка решает, что перечитывать. Срежь его консьюмер — реакция
        снова стоила бы трёх запросов с каждой открытой вкладки."""
        socket, _ = await self.open_socket(self.alice)
        await get_channel_layer().group_send(chat_group(self.chat.pk), {
            "type": "chat.event", "chat": self.chat.pk, "kind": "react",
        })
        self.assertEqual(await socket.receive_json_from(), {"chat": self.chat.pk, "kind": "react"})
        await socket.disconnect()

    async def test_stranger_gets_nothing(self):
        socket, _ = await self.open_socket(self.stranger)
        await get_channel_layer().group_send(chat_group(self.chat.pk), {"type": "chat.event", "chat": self.chat.pk})
        self.assertTrue(await socket.receive_nothing())
        await socket.disconnect()

    async def test_added_to_chat_while_online_resubscribes(self):
        """В группе нового чата сокета ещё нет — зовём по личной, он досоединяется."""
        socket, _ = await self.open_socket(self.alice)
        group = await database_sync_to_async(self.make_group)()

        await get_channel_layer().group_send(user_group(self.alice.pk), {"type": "chat.joined", "chat": group.pk})
        self.assertEqual(await socket.receive_json_from(), {"chat": group.pk})

        await get_channel_layer().group_send(chat_group(group.pk), {"type": "chat.event", "chat": group.pk})
        self.assertEqual(await socket.receive_json_from(), {"chat": group.pk})
        await socket.disconnect()

    def make_group(self):
        chat = Chat.objects.create(kind="group", title="Проект")
        Membership.objects.create(chat=chat, user=self.alice)
        return chat


class SendLimitTests(TestCase):
    """Одно сообщение расходится по вкладкам всех участников, а в чате курса их под сотню."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты
        self.client.force_login(self.alice)
        self.url = reverse("message_send", args=[self.chat.pk])

    def test_the_flood_stops_and_leaves_nothing_behind(self):
        for i in range(SEND_LIMIT):
            self.assertEqual(self.client.post(self.url, {"text": f"раз {i}"}).status_code, 200)
        self.assertEqual(self.client.post(self.url, {"text": "лишнее"}).status_code, 429)
        self.assertEqual(Message.objects.count(), SEND_LIMIT)

    def test_the_limit_is_personal(self):
        for i in range(SEND_LIMIT + 1):
            self.client.post(self.url, {"text": f"раз {i}"})
        self.client.force_login(self.bob)
        self.assertEqual(self.client.post(self.url, {"text": "а мне можно"}).status_code, 200)

    def test_reactions_are_limited_as_well(self):
        """Реакция расходится по чату таким же событием, как и сообщение: без предела
        одно нажатие в цикле поднимало бы на ноги все вкладки курса."""
        message = Message.objects.create(chat=self.chat, author=self.alice, text="раз")
        url = reverse("message_react", args=[message.pk])
        for _ in range(ACT_LIMIT):
            self.assertEqual(self.client.post(url, {"emoji": "👍"}).status_code, 200)
        self.assertEqual(self.client.post(url, {"emoji": "🔥"}).status_code, 429)

    def test_the_action_limit_is_shared_by_edits_and_deletes(self):
        message = Message.objects.create(chat=self.chat, author=self.alice, text="раз")
        react = reverse("message_react", args=[message.pk])
        for _ in range(ACT_LIMIT):
            self.client.post(react, {"emoji": "👍"})
        self.assertEqual(
            self.client.post(reverse("message_delete", args=[message.pk])).status_code, 429
        )
        self.assertFalse(Message.objects.get(pk=message.pk).deleted)


class CatchUpTests(TestCase):
    """Догон по курсору. Вкладку оставляют открытой на ночь, а чат курса за ночь живёт."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)
        cls.seen = Message.objects.create(chat=cls.chat, author=cls.bob, text="это мы видели")

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты
        self.client.force_login(self.alice)
        self.url = reverse("messages_new", args=[self.chat.pk])

    def flood(self, count):
        Message.objects.bulk_create(
            [Message(chat=self.chat, author=self.bob, text=f"№{i}") for i in range(count)]
        )

    def test_a_gap_we_can_stitch_arrives_as_messages(self):
        self.flood(CATCH_UP)
        response = self.client.get(self.url, {"after": self.seen.pk})
        self.assertNotIn("HX-Refresh", response.headers)
        self.assertContains(response, "№0")
        self.assertContains(response, f"№{CATCH_UP - 1}")
        self.assertEqual(response.content.decode().count('data-id="'), CATCH_UP)

    def test_the_limit_is_part_of_the_query(self):
        """Иначе ночная переписка сначала целиком приезжает в память и там же выбрасывается."""
        self.flood(CATCH_UP + 50)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.url, {"after": self.seen.pk})
        feed = [q["sql"] for q in ctx.captured_queries if "chats_message" in q["sql"]]
        self.assertTrue(any(f"LIMIT {CATCH_UP + 1}" in sql for sql in feed), feed)

    def test_a_gap_too_wide_asks_the_page_to_redraw(self):
        """Порциями такой разрыв не сшить, а у страницы своя пагинация."""
        self.flood(CATCH_UP + 1)
        response = self.client.get(self.url, {"after": self.seen.pk})
        self.assertEqual(response.headers.get("HX-Refresh"), "true")
        self.assertEqual(response.content, b"")

    def test_sending_across_a_wide_gap_redraws_too(self):
        """Своё сообщение уже записано — страница перечитает ленту вместе с ним."""
        self.flood(CATCH_UP + 1)
        response = self.client.post(
            reverse("message_send", args=[self.chat.pk]), {"text": "моё", "after": self.seen.pk}
        )
        self.assertEqual(response.headers.get("HX-Refresh"), "true")
        self.assertTrue(Message.objects.filter(text="моё").exists())


class DayDividerTests(TestCase):
    """Дата в ленте. До неё у сообщения были одни часы, и позавчерашнее выглядело сегодняшним."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты
        self.client.force_login(self.alice)

    def say(self, text, days_ago=0, author=None):
        message = Message.objects.create(chat=self.chat, author=author or self.bob, text=text)
        if days_ago:
            # не через create: `created` там заполняет default, и своё значение он затрёт
            Message.objects.filter(pk=message.pk).update(created=timezone.now() - timedelta(days=days_ago))
            message.refresh_from_db()
        return message

    def feed(self):
        return self.client.get(reverse("chat_detail", args=[self.chat.pk])).content.decode()

    def test_every_day_gets_its_own_line(self):
        self.say("позавчера", days_ago=2)
        self.say("вчера", days_ago=1)
        self.say("сегодня")
        page = self.feed()
        self.assertEqual(page.count("data-day="), 3)
        self.assertIn("Вчера", page)
        self.assertIn("Сегодня", page)

    def test_messages_of_one_day_share_one_line(self):
        for i in range(3):
            self.say(f"сегодня {i}")
        self.assertEqual(self.feed().count("data-day="), 1)

    def test_the_date_stands_once_even_when_history_comes_in_pieces(self):
        """Порция начинается посреди дня — дата над ней означала бы, что день начался тут."""
        for i in range(PAGE_SIZE + 5):
            self.say(f"сегодня {i}")
        # Всё прочитано: иначе чат открылся бы с первого непрочитанного, то есть с начала
        Membership.objects.filter(chat=self.chat, user=self.alice).update(
            last_read=Message.objects.latest("id")
        )
        self.assertNotIn("data-day=", self.feed())

        oldest_shown = Message.objects.order_by("-id")[PAGE_SIZE - 1]
        older = self.client.get(
            reverse("messages_older", args=[self.chat.pk]), {"before": oldest_shown.pk}
        ).content.decode()
        self.assertEqual(older.count("data-day="), 1)

    def test_a_new_message_of_the_same_day_brings_no_date(self):
        first = self.say("сегодня")
        self.say("тоже сегодня")
        answer = self.client.get(reverse("messages_new", args=[self.chat.pk]), {"after": first.pk})
        self.assertNotContains(answer, "data-day=")

    def test_the_first_message_after_midnight_brings_the_date(self):
        yesterday = self.say("вчера", days_ago=1)
        self.say("сегодня")
        answer = self.client.get(reverse("messages_new", args=[self.chat.pk]), {"after": yesterday.pk})
        self.assertContains(answer, "Сегодня")

    def test_a_redrawn_bubble_carries_no_date(self):
        """Пузырь подменяется на месте, а дата стоит соседним узлом — иначе она задвоится."""
        self.say("вчера", days_ago=1)
        today = self.say("сегодня")
        self.assertNotContains(self.client.get(reverse("message_card", args=[today.pk])), "data-day=")

    def test_an_oob_replacement_carries_no_date(self):
        self.say("вчера", days_ago=1)
        today = self.say("сегодня")
        Message.objects.filter(pk=today.pk).update(updated=timezone.now())
        answer = self.client.get(
            reverse("messages_new", args=[self.chat.pk]), {"after": today.pk, "since": today.pk}
        )
        self.assertContains(answer, "hx-swap-oob")
        self.assertNotContains(answer, "data-day=")


class PackTests(TestCase):
    """Пачки: подряд идущие сообщения одного автора рисуют аватар и имя один раз.
    Иначе каждое «ага» тащит за собой лицо и подпись, и лента выглядит рвано."""

    SPACER = '<span class="w-8 shrink-0"></span>'  # пустое место вместо аватара
    # Именно подпись, а не имя где угодно: оно есть и в data-author каждого пузыря —
    # оттуда его берёт меню, когда на сообщение отвечают.
    SIGNED = ">Бобров Борис</a>"

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local", "Алиса", "Аброва")
        cls.bob = make_user("b@t.local", "Борис", "Бобров")
        cls.chat = Chat.objects.create(kind="group", title="Проект")
        Membership.objects.bulk_create([
            Membership(chat=cls.chat, user=cls.alice), Membership(chat=cls.chat, user=cls.bob),
        ])

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты
        self.client.force_login(self.alice)

    def say(self, author, text):
        return Message.objects.create(chat=self.chat, author=author, text=text)

    def at(self, author, text, hour):
        """Сообщение в заданный час СЕГОДНЯШНЕГО дня.

        Не «столько-то назад»: два часа назад в половине первого ночи — это уже вчера,
        и тест про молчание проверял бы разрыв дня вместо разрыва пачки. Один раз он так
        и прошёл мимо сломанной проверки.
        """
        message = Message.objects.create(chat=self.chat, author=author, text=text)
        when = timezone.localtime().replace(hour=hour, minute=0, second=0, microsecond=0)
        Message.objects.filter(pk=message.pk).update(created=when)
        message.refresh_from_db()
        return message

    def feed(self):
        return self.client.get(reverse("chat_detail", args=[self.chat.pk])).content.decode()

    def card(self, message):
        return self.client.get(reverse("message_card", args=[message.pk])).content.decode()

    def test_a_run_of_one_author_is_signed_once(self):
        for i in range(3):
            self.say(self.bob, f"раз {i}")
        page = self.feed()
        self.assertEqual(page.count(self.SIGNED), 1)
        self.assertEqual(page.count(self.SPACER), 2)

    def test_another_author_starts_a_new_pack(self):
        self.say(self.bob, "первое")
        self.say(self.alice, "второе")
        self.assertNotIn(self.SPACER, self.feed())

    def test_a_long_silence_starts_a_new_pack(self):
        """Вернулся человек через час — это уже другой разговор, подпись нужна заново.
        День при этом один и тот же: проверяем разрыв пачки, а не разрыв суток."""
        self.at(self.bob, "утром", 10)
        self.at(self.bob, "днём", 13)
        self.assertNotIn(self.SPACER, self.feed())

    def test_a_redrawn_bubble_remembers_its_pack(self):
        """Пузырь подменяется целиком: забудь он про пачку — аватар пропадал бы от реакции."""
        first = self.say(self.bob, "первое")
        second = self.say(self.bob, "второе")
        self.assertNotIn(self.SPACER, self.card(first))
        self.assertIn(self.SPACER, self.card(second))

    def test_the_seam_between_batches_does_not_repeat_the_avatar(self):
        """Порция приезжает без соседей — без оглядки на них она начинала бы пачку заново."""
        first = self.say(self.bob, "первое")
        self.say(self.bob, "второе")
        answer = self.client.get(reverse("messages_new", args=[self.chat.pk]), {"after": first.pk})
        self.assertContains(answer, self.SPACER)

    def test_an_oob_replacement_remembers_its_pack_too(self):
        """Правят как раз первое сообщение пачки — без пометок оно теряло бы аватар и имя."""
        first = self.say(self.bob, "первое")
        second = self.say(self.bob, "второе")
        Message.objects.filter(pk=first.pk).update(updated=timezone.now())
        answer = self.client.get(
            reverse("messages_new", args=[self.chat.pk]), {"after": second.pk, "since": first.pk}
        )
        self.assertContains(answer, "hx-swap-oob")
        self.assertContains(answer, self.SIGNED)
        self.assertNotContains(answer, self.SPACER)

    def test_a_dialogue_needs_no_names(self):
        """Собеседник один и он в шапке — подпись в каждом пузыре была бы шумом.
        Аватар при этом остаётся: без него своё от чужого отличал бы только оттенок."""
        dm = Chat.get_or_create_dm(self.alice, self.bob)
        message = Message.objects.create(chat=dm, author=self.bob, text="привет")
        card = self.client.get(reverse("message_card", args=[message.pk])).content.decode()
        self.assertNotIn(self.SIGNED, card)
        self.assertIn(f'href="{reverse("profile", args=[self.bob.pk])}" class="shrink-0"', card)

    def test_a_group_signs_who_is_speaking(self):
        self.assertIn(self.SIGNED, self.card(self.say(self.bob, "привет")))

    def test_my_own_message_is_signed_as_mine(self):
        """Своё отличается цветом пузыря и подписью, а не стороной экрана."""
        self.assertIn(">Вы</a>", self.card(self.say(self.alice, "моё")))

    def test_a_quote_is_signed_like_the_bubble_itself(self):
        """Раньше в цитате стояло одно имя, а в подписи — Фамилия Имя. Разнобой на виду."""
        mine = self.say(self.alice, "моё")
        theirs = self.say(self.bob, "чужое")
        answer = Message.objects.create(chat=self.chat, author=self.bob, text="ответ", reply_to=mine)
        self.assertIn(">Вы</span>", self.card(answer))

        answer.reply_to = theirs
        answer.save(update_fields=["reply_to"])
        self.assertIn(">Бобров Борис</span>", self.card(answer))


class OnlineTests(TestCase):
    """«В сети» в шапке. Отметка активности освежается раз в пять минут, поэтому это
    «заходил только что», и окно взято вдвое шире — иначе половина сидящих в него не попадёт."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.objects.create(kind="group", title="Проект")
        Membership.objects.bulk_create([
            Membership(chat=cls.chat, user=cls.alice), Membership(chat=cls.chat, user=cls.bob),
        ])

    def test_the_header_counts_who_is_around(self):
        self.client.force_login(self.alice)  # вход заводит запись сессии с отметкой «сейчас»
        self.assertContains(self.client.get(reverse("chat_detail", args=[self.chat.pk])), "1 в сети")

    def test_yesterdays_visit_does_not_count(self):
        self.client.force_login(self.alice)
        UserSession.objects.update(seen=timezone.now() - timedelta(days=1))
        self.assertNotContains(self.client.get(reverse("chat_detail", args=[self.chat.pk])), "в сети")


class UnreadOpenTests(TestCase):
    """Чат открывается там, где человек остановился, а не в конце переписки."""

    MARK = "data-unread"

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def setUp(self):
        self.client.force_login(self.alice)
        self.url = reverse("chat_detail", args=[self.chat.pk])

    def flood(self, count, author=None):
        return Message.objects.bulk_create(
            [Message(chat=self.chat, author=author or self.bob, text=f"№{i}") for i in range(count)]
        )

    def read_all(self):
        Membership.objects.filter(chat=self.chat, user=self.alice).update(
            last_read=Message.objects.latest("id")
        )

    def test_the_line_stands_before_the_first_unread(self):
        self.flood(3)
        seen = Message.objects.order_by("id").first()
        Membership.objects.filter(chat=self.chat, user=self.alice).update(last_read=seen)
        page = self.client.get(self.url).content.decode()
        self.assertEqual(page.count(self.MARK), 1)
        self.assertLess(page.index("№1"), page.index("№2"))  # черта между ними, порядок цел
        self.assertLess(page.index(self.MARK), page.index("№1"))

    def test_everything_read_means_no_line(self):
        self.flood(3)
        self.read_all()
        self.assertNotContains(self.client.get(self.url), self.MARK)

    def test_my_own_message_is_not_unread(self):
        """Черта над собственной репликой выглядела бы так, будто себя не читали."""
        self.flood(2)
        self.read_all()
        Message.objects.create(chat=self.chat, author=self.alice, text="моё")
        self.assertNotContains(self.client.get(self.url), self.MARK)

    def test_a_night_of_unread_opens_at_the_start_of_it(self):
        """Иначе после ночи в чате курса человек попадает в конец и листает назад руками."""
        self.flood(PAGE_SIZE + 20)
        page = self.client.get(self.url).content.decode()  # один раз: он же помечает прочитанным
        self.assertIn("№0", page)  # самое старое непрочитанное
        self.assertEqual(page.count(self.MARK), 1)

    def test_a_week_of_unread_opens_as_usual(self):
        """Столько непрочитанных — значит человека не было неделю; простыня тут не поможет."""
        self.flood(CATCH_UP + 5)
        page = self.client.get(self.url).content.decode()
        self.assertNotIn("№0", page)  # открылись концом
        self.assertNotIn(self.MARK, page)

    def test_the_limit_is_part_of_the_query(self):
        """Иначе непрочитанное за неделю сначала целиком приезжает в память и там же
        выбрасывается — а решение открыться концом уже принято."""
        self.flood(CATCH_UP + 5)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.url)
        feed = [q["sql"] for q in ctx.captured_queries if "chats_message" in q["sql"]]
        self.assertTrue(any(f"LIMIT {CATCH_UP + 1}" in sql for sql in feed), feed)

    def test_opening_marks_what_it_showed_as_read(self):
        self.flood(3)
        self.client.get(self.url)
        membership = Membership.objects.get(chat=self.chat, user=self.alice)
        self.assertEqual(membership.last_read_id, Message.objects.latest("id").pk)


class ReadersTests(TestCase):
    """«Кто прочитал» — по нажатию. Живых галочек нет намеренно: чтобы они не врали,
    курсор чтения каждого пришлось бы рассылать всем на каждый опрос."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local", "Алиса", "Аброва")
        cls.bob = make_user("b@t.local", "Борис", "Бобров")
        cls.carol = make_user("c@t.local", "Вера", "Волкова")
        cls.chat = Chat.objects.create(kind="group", title="Проект")
        Membership.objects.bulk_create([
            Membership(chat=cls.chat, user=cls.alice),
            Membership(chat=cls.chat, user=cls.bob),
            Membership(chat=cls.chat, user=cls.carol),
        ])
        cls.earlier = Message.objects.create(chat=cls.chat, author=cls.bob, text="раньше")
        cls.message = Message.objects.create(chat=cls.chat, author=cls.alice, text="моё")

    def setUp(self):
        self.client.force_login(self.alice)
        self.url = reverse("message_readers", args=[self.message.pk])

    def read_by(self, user):
        Membership.objects.filter(chat=self.chat, user=user).update(last_read=self.message)

    def test_nobody_yet(self):
        self.assertContains(self.client.get(self.url), "Пока никто не прочитал")

    def test_counts_out_of_everyone_but_me(self):
        self.read_by(self.bob)
        response = self.client.get(self.url)
        self.assertContains(response, "Прочитали 1 из 2")
        self.assertContains(response, "Бобров Борис")
        self.assertNotContains(response, "Волкова Вера")

    def test_an_older_cursor_does_not_count(self):
        """Курсор стоит на прежнем сообщении — значит до этого человек не дочитал."""
        Membership.objects.filter(chat=self.chat, user=self.bob).update(last_read=self.earlier)
        self.assertContains(self.client.get(self.url), "Пока никто не прочитал")

    def test_a_dialogue_answers_yes_or_no(self):
        """В ЛС собеседник один: список из одного человека вместо ответа — канцелярия."""
        dm = Chat.get_or_create_dm(self.alice, self.bob)
        mine = Message.objects.create(chat=dm, author=self.alice, text="моё")
        url = reverse("message_readers", args=[mine.pk])
        self.assertContains(self.client.get(url), "Ещё не прочитано")

        Membership.objects.filter(chat=dm, user=self.bob).update(last_read=mine)
        response = self.client.get(url)
        self.assertContains(response, "Прочитано")
        self.assertNotContains(response, "Бобров Борис")  # списка из одного тут не нужно

    def test_a_dialogue_shows_ticks_in_the_bubble(self):
        """В ЛС прочтение — одно событие одному человеку, и оно окупает живую галочку.
        Спрашивать про неё через меню значило бы два клика ради ответа «да» или «нет»."""
        dm = Chat.get_or_create_dm(self.alice, self.bob)
        mine = Message.objects.create(chat=dm, author=self.alice, text="моё")
        # Именно у галочки, а не где угодно на странице: fa-check-double есть и в меню
        one = f'<i data-tick="{mine.pk}" class="fa-solid ml-1 fa-check">'
        both = f'<i data-tick="{mine.pk}" class="fa-solid ml-1 fa-check-double text-accent">'
        self.assertContains(self.client.get(reverse("chat_detail", args=[dm.pk])), one)

        Membership.objects.filter(chat=dm, user=self.bob).update(last_read=mine)
        self.assertContains(self.client.get(reverse("chat_detail", args=[dm.pk])), both)

    def test_a_group_has_no_ticks(self):
        """Иначе курсор каждого пришлось бы рассылать всем на каждый опрос."""
        message = Message.objects.create(chat=self.chat, author=self.alice, text="в группу")
        self.assertNotContains(self.client.get(reverse("message_card", args=[message.pk])), "data-tick")

    def test_a_group_poll_does_not_ask_about_cursors(self):
        """Галочек в группе нет, значит и курсор собеседника спрашивать не за чем —
        а опрос идёт у каждой вкладки каждого участника."""
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(
                reverse("messages_new", args=[self.chat.pk]),
                {"after": self.message.pk}, headers={"HX-Request": "true"},
            )
        asked = [q["sql"] for q in ctx.captured_queries if 'SELECT "chats_membership"."last_read_id" AS' in q["sql"]]
        self.assertEqual(asked, [])

    def test_reading_a_dialogue_tells_the_other_side(self):
        self.client.force_login(self.bob)
        dm = Chat.get_or_create_dm(self.alice, self.bob)
        mine = Message.objects.create(chat=dm, author=self.alice, text="моё")
        with mock.patch("chats.views.notify_read") as notify:
            self.client.get(reverse("chat_detail", args=[dm.pk]))
        notify.assert_called_once_with(dm.pk, self.bob.pk, mine.pk)

    def test_reading_a_group_stays_quiet(self):
        Message.objects.create(chat=self.chat, author=self.alice, text="в группу")
        self.client.force_login(self.bob)
        with mock.patch("chats.views.notify_read") as notify:
            self.client.get(reverse("chat_detail", args=[self.chat.pk]))
        notify.assert_not_called()

    def test_only_the_author_may_ask(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_a_stranger_gets_nothing(self):
        self.client.force_login(make_user("s@t.local"))
        self.assertEqual(self.client.get(self.url).status_code, 404)


class LinkTests(TestCase):
    """Ссылками в чате перекидываются постоянно — до этого их выделяли и копировали руками."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def bubble(self, text):
        message = Message.objects.create(chat=self.chat, author=self.bob, text=text)
        self.client.force_login(self.alice)
        return self.client.get(reverse("message_card", args=[message.pk])).content.decode()

    def test_a_link_becomes_clickable_in_a_new_tab(self):
        page = self.bubble("разбор тут https://knt-mipt.ru/materials/")
        self.assertIn('href="https://knt-mipt.ru/materials/"', page)
        self.assertIn('target="_blank"', page)

    def test_markup_in_the_text_stays_text(self):
        """urlize сначала экранирует — иначе сообщение стало бы способом писать разметку."""
        page = self.bubble('<script>alert(1)</script> и <b>жирным</b>')
        self.assertNotIn("<script>", page)
        self.assertNotIn("<b>жирным</b>", page)
        self.assertIn("&lt;script&gt;", page)


class ComposerTests(TestCase):
    """Поле ввода. Было однострочным: абзац набрать нельзя вовсе — при том что пузырь
    переносы показывать умеет, а форма правки сообщения и так была textarea."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)
        cls.mine = Message.objects.create(chat=cls.chat, author=cls.alice, text="моё")

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты
        self.client.force_login(self.alice)

    def send(self, text):
        self.client.post(reverse("message_send", args=[self.chat.pk]), {"text": text})
        return Message.objects.latest("id").text

    def test_the_field_takes_more_than_one_line(self):
        self.assertContains(self.client.get(reverse("chat_detail", args=[self.chat.pk])), '<textarea name="text"')

    def test_the_field_limit_comes_from_the_server(self):
        """4000 стояло строкой в двух шаблонах, и разъехаться с сервером ему ничто не мешало."""
        for url in (reverse("chat_detail", args=[self.chat.pk]), reverse("message_edit", args=[self.mine.pk])):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), f'maxlength="{MAX_TEXT}"')

    def test_line_breaks_survive(self):
        self.assertEqual(self.send("первая строка\nвторая строка"), "первая строка\nвторая строка")

    def test_a_wall_of_empty_lines_is_folded(self):
        """Иначе сообщение из четырёх тысяч переносов растянуло бы ленту у всех, кто в чате."""
        self.assertEqual(self.send("верх" + "\n" * 50 + "низ"), "верх\n\nниз")

    def test_spaces_do_not_smuggle_that_wall_through(self):
        self.assertEqual(self.send("верх" + "\n   " * 50 + "низ"), "верх\n\nниз")

    def test_the_editor_folds_them_too(self):
        self.client.post(reverse("message_edit", args=[self.mine.pk]), {"text": "верх" + "\n" * 9 + "низ"})
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.text, "верх\n\nниз")


def picture(name="photo.webp", side=8, colour="red"):
    """Настоящая картинка файлом: ImageField проверяет содержимое, а не расширение."""
    buffer = BytesIO()
    PilImage.new("RGB", (side, side), colour).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class AttachmentTests(TestCase):
    """Вложения сообщения. Идут обычным multipart вместе с самим сообщением: запись
    заводится в том же запросе, и в хранилище не остаётся сирот."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты
        self.client.force_login(self.alice)
        self.url = reverse("message_send", args=[self.chat.pk])

    def test_a_photo_arrives_with_its_preview(self):
        response = self.client.post(self.url, {
            "text": "смотри", "photo": picture("shot.webp"), "preview": picture("shot.thumb.webp"),
        })
        self.assertEqual(response.status_code, 200)
        image = Image.objects.get()
        self.assertEqual(image.message, Message.objects.get())
        self.assertTrue(image.image and image.preview)
        self.assertEqual(image.uploader, self.alice)

    def test_a_document_arrives_as_a_file(self):
        self.client.post(self.url, {"doc": SimpleUploadedFile("конспект.pdf", b"%PDF-1.4\n")})
        file = File.objects.get()
        self.assertEqual(file.name, "конспект.pdf")
        self.assertEqual(file.message, Message.objects.get())

    def test_an_attachment_needs_no_caption(self):
        """Фотографию отправляют молча чаще, чем с подписью."""
        self.client.post(self.url, {"photo": picture()})
        self.assertEqual(Message.objects.get().text, "")

    def test_nothing_at_all_is_still_nothing(self):
        self.assertEqual(self.client.post(self.url, {"text": "  "}).status_code, 204)
        self.assertFalse(Message.objects.exists())

    def test_too_many_at_once_are_refused_whole(self):
        response = self.client.post(self.url, {
            "text": "куча", "doc": [SimpleUploadedFile(f"{i}.txt", b"x") for i in range(MAX_FILES + 1)],
        })
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Message.objects.exists())  # сообщение без вложений тоже не заводим

    def test_a_forbidden_type_is_refused(self):
        """Медиа отдаётся с домена сайта, и html оттуда выполнился бы как его код."""
        response = self.client.post(self.url, {"doc": SimpleUploadedFile("hack.html", b"<script>")})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(File.objects.exists())

    def test_a_photo_over_the_limit_is_refused(self):
        """Предел подменяем, а не льём десять мегабайт: картинка должна остаться настоящей,
        иначе отказ придёт не за размер, а за то, что это не картинка."""
        with mock.patch("chats.uploads.MAX_PHOTO", 10):
            response = self.client.post(self.url, {"photo": picture()})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Image.objects.exists())

    def test_a_document_over_the_limit_is_refused(self):
        with mock.patch("chats.uploads.MAX_DOC", 10):
            response = self.client.post(self.url, {"doc": SimpleUploadedFile("много.txt", b"x" * 50)})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(File.objects.exists())

    def test_something_that_is_not_a_picture_is_refused(self):
        """Расширение ничего не доказывает: модельный ImageField содержимое не проверяет,
        и без своей проверки «фотографией» стал бы любой файл с подходящим именем."""
        response = self.client.post(self.url, {"photo": SimpleUploadedFile("fake.webp", b"not a picture")})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Image.objects.exists())
        self.assertFalse(Message.objects.exists())

    def test_a_preview_that_is_not_a_picture_is_refused(self):
        """Поле у миниатюры своё, а хранилище общее. Без проверки через него в бакет
        уезжал бы файл любого содержимого — «картинкой» он был бы только по имени поля."""
        response = self.client.post(self.url, {
            "photo": picture(), "preview": SimpleUploadedFile("evil.html", b"<script>alert(1)</script>"),
        })
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Image.objects.exists())
        self.assertFalse(Message.objects.exists())

    def test_a_preview_over_the_limit_is_refused(self):
        with mock.patch("chats.uploads.MAX_PHOTO", 10):
            response = self.client.post(self.url, {"photo": picture(side=1), "preview": picture(side=8)})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Image.objects.exists())

    def test_more_previews_than_photos_are_refused(self):
        """Пары им ставит поле ввода, лишним взяться неоткуда: значит запрос не наш."""
        response = self.client.post(self.url, {
            "photo": picture(), "preview": [picture("a.webp"), picture("b.webp")],
        })
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Image.objects.exists())

    def test_attachments_show_up_in_the_feed(self):
        self.client.post(self.url, {"photo": picture(), "preview": picture("t.webp")})
        page = self.client.get(reverse("chat_detail", args=[self.chat.pk])).content.decode()
        self.assertIn("lightbox = ", page)  # картинка открывается на весь экран

    def test_deleting_the_message_takes_the_attachments(self):
        self.client.post(self.url, {"doc": SimpleUploadedFile("файл.txt", b"x")})
        Message.objects.get().delete()
        self.assertFalse(File.objects.exists())


class ChatListLooksTests(TestCase):
    """Мелочи списка и шапки, из-за которых чат выглядел неряшливо."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.carol = make_user("c@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)

    def setUp(self):
        self.client.force_login(self.alice)

    def test_yesterdays_message_is_not_dressed_as_todays(self):
        message = Message.objects.create(chat=self.chat, author=self.bob, text="привет")
        Message.objects.filter(pk=message.pk).update(created=timezone.now() - timedelta(days=1))
        Chat.objects.filter(pk=self.chat.pk).update(last_message=message)
        page = self.client.get(reverse("chat_list")).content.decode()
        self.assertIn("вчера", page)

    def test_the_freshest_talk_is_on_top(self):
        """По времени, а не по id: импорт старого сайта и демка заводят переписку задним
        числом, и по id она выстраивалась в порядке заливки, а не разговора."""
        old = Chat.objects.create(kind="group", title="Давняя группа")
        Membership.objects.create(chat=old, user=self.alice)
        fresh = Message.objects.create(chat=self.chat, author=self.bob, text="сегодня")
        stale = Message.objects.create(chat=old, author=self.alice, text="год назад")
        Message.objects.filter(pk=stale.pk).update(created=timezone.now() - timedelta(days=365))
        Chat.objects.filter(pk=self.chat.pk).update(last_message=fresh)
        Chat.objects.filter(pk=old.pk).update(last_message=stale)

        page = self.client.get(reverse("chat_list")).content.decode()
        self.assertLess(page.index("сегодня"), page.index("Давняя группа"))

    def test_the_member_count_is_declined(self):
        chat = Chat.objects.create(kind="group", title="Проект")
        Membership.objects.bulk_create([
            Membership(chat=chat, user=self.alice), Membership(chat=chat, user=self.bob),
        ])
        self.assertContains(self.client.get(reverse("chat_detail", args=[chat.pk])), "2 участника ")

        Membership.objects.bulk_create(
            [Membership(chat=chat, user=make_user(f"n{i}@t.local")) for i in range(3)]
        )
        self.assertContains(self.client.get(reverse("chat_detail", args=[chat.pk])), "5 участников ")

    def test_a_wordless_photo_is_not_an_empty_line(self):
        """Сообщение бывает из одних вложений, а строка в списке показывала его текст —
        то есть пустоту, и чат выглядел как чат без сообщений."""
        cache.clear()
        self.client.post(reverse("message_send", args=[self.chat.pk]), {"photo": picture()})
        self.assertContains(self.client.get(reverse("chat_list")), "Вы: Фото")

    def test_a_wordless_document_is_named(self):
        cache.clear()
        self.client.post(
            reverse("message_send", args=[self.chat.pk]),
            {"doc": SimpleUploadedFile("конспект.pdf", b"%PDF-1.4\n")},
        )
        self.assertContains(self.client.get(reverse("chat_list")), "конспект.pdf")

    def test_the_list_reads_members_of_direct_chats_only(self):
        """Собеседник нужен только в ЛС, а участников читали у всех чатов подряд —
        в чате курса это сотни людей за раз, и все они тут же выбрасывались."""
        talk = Message.objects.create(chat=self.chat, author=self.bob, text="привет")
        Chat.objects.filter(pk=self.chat.pk).update(last_message=talk)
        course = Chat.objects.create(kind="course", title="Курс", admission_year=2024, stage="bachelor")
        Membership.objects.create(chat=course, user=self.alice)
        crowd = [make_user(f"c{i}@t.local", surname=f"Ц{i}") for i in range(20)]
        Membership.objects.bulk_create([Membership(chat=course, user=person) for person in crowd])
        last = Message.objects.create(chat=course, author=crowd[0], text="привет курсу")
        Chat.objects.filter(pk=course.pk).update(last_message=last)

        loaded = sum(len(item.chat.memberships.all()) for item in _chat_items(self.alice))
        self.assertEqual(loaded, 2, "участники группового чата читаются зря")

    def test_a_vanished_chat_says_so_on_the_list(self):
        """Вкладку с исчезнувшим чатом уводит сюда: 404 в ответ на догрузку — не объяснение."""
        response = self.client.get(reverse("chat_list"), {"gone": "1"}, follow=True)
        self.assertRedirects(response, reverse("chat_list"))
        self.assertContains(response, "Чат больше недоступен")


class TemplateHealthTests(TestCase):
    """{# ... #} в Django однострочный: перенос строки — и комментарий уезжает в вёрстку."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("a@t.local")
        cls.bob = make_user("b@t.local")
        cls.chat = Chat.get_or_create_dm(cls.alice, cls.bob)
        Message.objects.create(chat=cls.chat, author=cls.bob, text="привет")

    def setUp(self):
        self.client.force_login(self.alice)

    def test_no_multiline_comments_in_any_template(self):
        """Ловим в исходниках, а не в вёрстке: страницу с таким комментарием
        могут просто не открыть в тестах, а текст всё равно уедет пользователю."""
        root = Path(settings.BASE_DIR)
        broken = [
            f"{path.relative_to(root)}:{number}"
            for path in root.glob("*/templates/**/*.html")
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if line.count("{#") != line.count("#}")
        ]
        self.assertFalse(broken, "многострочный {# #} рендерится как текст — нужен {% comment %}: " + ", ".join(broken))

    def test_pages_render_without_leaking_template_comments(self):
        for url in (reverse("chat_list"), reverse("chat_detail", args=[self.chat.pk])):
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url), "{#")

    def test_fragments_render_without_leaking_template_comments(self):
        urls = (
            reverse("messages_new", args=[self.chat.pk]),
            reverse("chat_list_fragment"),
            reverse("unread_badge"),
            reverse("user_search"),
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url, headers={"HX-Request": "true"}), "{#")
