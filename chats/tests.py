from pathlib import Path
from unittest import mock

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Permission
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Team
from users.models import User

from .consumers import ChatConsumer
from .events import chat_group, user_group
from .models import Chat, Membership, Message, unread_total


def make_user(email, name="Иван", surname="Иванов", **extra):
    extra.setdefault("must_change_password", False)  # иначе middleware уведёт на смену пароля
    return User.objects.create_user(email=email, name=name, surname=surname, password="pass12345", **extra)


def make_team(number, stage="bachelor", year=2024):
    return Team.objects.create(
        number=number, profile="Прикладная математика", course_code="03.03.01",
        stage=stage, year_of_admission=year,
    )


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
        response = self.client.get(reverse("dm_start", args=[self.bob.pk]))
        chat = Chat.objects.get(kind="dm")
        self.assertRedirects(response, reverse("chat_detail", args=[chat.pk]))

    def test_dm_start_with_self_goes_back_to_list(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse("dm_start", args=[self.alice.pk]))
        self.assertRedirects(response, reverse("chat_list"))
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
    """Открываем последнюю страницу, остальное подтягиваем маячком сверху."""

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
        response = self.client.get(self.url, {"after": self.message.pk})
        self.assertContains(response, "hx-swap-oob")
        self.assertContains(response, "исправил")

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
        self.assertContains(response, "🔥 1")

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
        self.client.force_login(self.alice)

    def group(self):
        chat = Chat.objects.create(kind="group", title="Проект")
        Membership.objects.create(chat=chat, user=self.alice, is_admin=True)
        Membership.objects.create(chat=chat, user=self.bob)
        return chat

    def test_send_notifies_chat(self):
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("message_send", args=[self.dm.pk]), {"text": "ещё"})
        notify.assert_called_once_with(self.dm.pk)

    def test_reaction_and_delete_notify_chat(self):
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("message_react", args=[self.message.pk]), {"emoji": "🔥"})
            self.client.post(reverse("message_delete", args=[self.message.pk]))
        self.assertEqual(notify.call_args_list, [mock.call(self.dm.pk), mock.call(self.dm.pk)])

    def test_edit_notifies_chat(self):
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("message_edit", args=[self.message.pk]), {"text": "правка"})
        notify.assert_called_once_with(self.dm.pk)

    def test_edit_without_changes_stays_quiet(self):
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("message_edit", args=[self.message.pk]), {"text": self.message.text})
        notify.assert_not_called()

    def test_new_dm_notifies_the_other_side(self):
        """Собеседник не подписан на группу чата, которого секунду назад не было."""
        with mock.patch("chats.views.notify_joined") as notify:
            self.client.get(reverse("dm_start", args=[self.carol.pk]))
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

    def test_delete_notifies_before_the_chat_is_gone(self):
        chat = self.group()
        with mock.patch("chats.views.notify_chat") as notify:
            self.client.post(reverse("chat_delete", args=[chat.pk]))
        notify.assert_called_once_with(chat.pk)
        self.assertFalse(Chat.objects.filter(pk=chat.pk).exists())


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
