from unittest import mock

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from attachments.models import File
from library.models import Book
from users.models import User


def make_user(email):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


class ModerationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")
        cls.stranger = make_user("s@t.local")
        cls.moderator = make_user("m@t.local")
        cls.moderator.user_permissions.add(
            Permission.objects.get(codename="change_book", content_type__app_label="library")
        )

    def book(self, title="Механика", status=Book.Status.PENDING):
        book = Book.objects.create(title=title, status=status, uploader=self.author)
        File.objects.create(book=book, name="скан.pdf", file=SimpleUploadedFile("скан.pdf", b"x"))
        return book

    def review(self, book, **data):
        return self.client.post(reverse("book_review", args=[book.pk]), data)

    # ── очередь ────────────────────────────────────────────────────────────────
    def test_queue_shows_what_waits(self):
        self.book(title="Ждущая")
        self.book(title="Уже вышла", status=Book.Status.APPROVED)
        self.client.force_login(self.moderator)

        response = self.client.get(reverse("review_queue"))
        self.assertContains(response, "Ждущая")
        self.assertNotContains(response, "Уже вышла")

    def test_queue_is_closed_for_everyone_else(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(reverse("review_queue")).status_code, 403)

    def test_sidebar_link_only_for_moderation(self):
        self.book()
        self.client.force_login(self.moderator)
        self.assertContains(self.client.get(reverse("book_list")), reverse("review_queue"))

        self.client.force_login(self.stranger)
        self.assertNotContains(self.client.get(reverse("book_list")), reverse("review_queue"))

    # ── решения ────────────────────────────────────────────────────────────────
    def test_approve_publishes_and_records_who_did_it(self):
        book = self.book()
        self.client.force_login(self.moderator)

        self.review(book, decision="approve")

        book.refresh_from_db()
        self.assertTrue(book.is_published)
        self.assertEqual(book.reviewed_by, self.moderator)
        self.assertIsNotNone(book.reviewed_at)

    def test_reject_keeps_the_reason_for_the_author(self):
        book = self.book()
        self.client.force_login(self.moderator)

        self.review(book, decision="reject", note="Скан нечитаемый")

        book.refresh_from_db()
        self.assertEqual(book.status, Book.Status.REJECTED)
        self.assertEqual(book.review_note, "Скан нечитаемый")

        # Автор видит причину у себя на странице книги, остальные книгу вообще не видят.
        self.client.force_login(self.author)
        self.assertContains(self.client.get(book.get_absolute_url()), "Скан нечитаемый")
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(book.get_absolute_url()).status_code, 404)

    def test_stranger_cannot_decide(self):
        book = self.book()
        self.client.force_login(self.stranger)

        self.assertEqual(self.review(book, decision="approve").status_code, 403)
        book.refresh_from_db()
        self.assertTrue(book.is_pending)

    def test_queue_answers_htmx_with_a_replacement_for_the_card(self):
        book = self.book()
        self.client.force_login(self.moderator)

        response = self.client.post(
            reverse("book_review", args=[book.pk]), {"decision": "approve"}, headers={"HX-Request": "true"},
        )
        self.assertContains(response, "опубликована")
        self.assertNotContains(response, "<html")

    # ── возврат на проверку ────────────────────────────────────────────────────
    def test_edit_by_the_author_sends_the_book_back(self):
        book = self.book(status=Book.Status.APPROVED)
        book.approve(self.moderator)
        book.save(update_fields=Book.REVIEW_FIELDS)
        self.client.force_login(self.author)

        self.client.post(reverse("book_edit", args=[book.pk]), {"title": "Другое название"})

        book.refresh_from_db()
        self.assertTrue(book.is_pending)
        self.assertIsNone(book.reviewed_by)  # прошлое решение отменено, а не осталось висеть

    def test_edit_by_moderation_keeps_it_published(self):
        book = self.book(status=Book.Status.APPROVED)
        self.client.force_login(self.moderator)

        self.client.post(reverse("book_edit", args=[book.pk]), {"title": "Другое название"})

        book.refresh_from_db()
        self.assertTrue(book.is_published)

    def test_moderation_fixing_a_rejected_book_publishes_it(self):
        book = self.book()
        book.reject(self.moderator, "Скан нечитаемый")
        book.save(update_fields=Book.REVIEW_FIELDS)
        self.client.force_login(self.moderator)

        self.client.post(reverse("book_edit", args=[book.pk]), {"title": "Другое название"})

        # Замечание модератор устранил сам — держать книгу отклонённой больше не за что.
        book.refresh_from_db()
        self.assertTrue(book.is_published)
        self.assertEqual(book.review_note, "")  # причина отказа снята вместе со статусом

    def test_moderation_editing_a_waiting_book_does_not_decide_for_it(self):
        # Поправить опечатку и продолжить проверку — не то же самое, что одобрить.
        book = self.book()
        self.client.force_login(self.moderator)

        with mock.patch("library.views.notify") as notify:
            self.client.post(reverse("book_edit", args=[book.pk]), {"title": "Другое название"})

        book.refresh_from_db()
        self.assertTrue(book.is_pending)
        notify.assert_not_called()  # книга и так в очереди, дёргать чат незачем

    # ── телеграм ───────────────────────────────────────────────────────────────
    def test_moderation_chat_hears_about_a_new_book(self):
        self.client.force_login(self.author)
        with mock.patch("library.views.notify") as notify:
            self.client.post(reverse("book_new"), {
                "title": "Механика", "files": SimpleUploadedFile("скан.pdf", b"x"),
            })

        chat, template, context = notify.call_args.args
        self.assertEqual(chat, "moderation")
        self.assertEqual(template, "telegram/book_pending.html")
        self.assertTrue(context["created"])
        self.assertIn("/library/", context["url"])

    def test_moderator_publishing_at_once_bothers_nobody(self):
        self.client.force_login(self.moderator)
        with mock.patch("library.views.notify") as notify:
            self.client.post(reverse("book_new"), {
                "title": "Механика", "files": SimpleUploadedFile("скан.pdf", b"x"),
            })

        notify.assert_not_called()
