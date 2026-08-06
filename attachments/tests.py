"""Прямая загрузка в хранилище: подпись ссылки и приём уже загруженного файла.

Владельцем берём книгу — она проще всех, но проверяется код attachments.
"""
import json
import os
from unittest import mock

from django.core import signing
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from library.models import Book
from users.models import User

from .models import File
from .storage import file_storage
from .uploads import UPLOAD_SALT


def make_user(email="u@t.local"):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


def fake_storage():
    """Storage с боевым API подписи: настоящий S3Storage в тестах не поднять."""
    storage = mock.MagicMock()
    storage.location = ""
    storage.bucket_name = "knt-files"
    storage.connection.meta.client.generate_presigned_url.return_value = "https://r2.example/put?sig=1"
    return storage


@override_settings(R2_BUCKET="knt-files")
class PresignTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user()

    def setUp(self):
        self.client.force_login(self.user)

    def ask(self, name="Зорич том 1.pdf", size=1024):
        with mock.patch("attachments.uploads.file_storage", fake_storage):
            return self.client.post(
                reverse("upload_url"), json.dumps({"name": name, "size": size}),
                content_type="application/json",
            )

    def test_returns_url_and_token(self):
        response = self.ask()
        data = response.json()
        self.assertEqual(data["url"], "https://r2.example/put?sig=1")

        payload = signing.loads(data["token"], salt=UPLOAD_SALT)
        self.assertEqual(payload["name"], "Зорич том 1.pdf")
        # Ключ выбирает сервер: свой прислать нельзя, а имя остаётся читаемым.
        self.assertRegex(payload["key"], r"^uploads/[0-9a-f]{32}/Зорич том 1\.pdf$")

    def test_dangerous_and_huge_are_refused(self):
        self.assertEqual(self.ask(name="страница.html").status_code, 400)
        with mock.patch("attachments.views.MAX_DIRECT_SIZE", 10):
            self.assertEqual(self.ask(size=100).status_code, 400)

    def test_garbage_request(self):
        self.assertEqual(
            self.client.post(reverse("upload_url"), "не json", content_type="application/json").status_code, 400
        )
        self.assertEqual(self.client.get(reverse("upload_url")).status_code, 405)

    @override_settings(R2_BUCKET="")
    def test_unavailable_without_r2(self):
        self.assertEqual(self.ask().status_code, 409)


class AdoptUploadTests(TestCase):
    """Форма присылает подписанный токен вместо файла — файл уже в хранилище."""

    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")

    def setUp(self):
        self.client.force_login(self.author)

    def put(self, key="uploads/abc/Зорич.pdf", body=b"pdf-payload"):
        return file_storage().save(key, ContentFile(body))

    def token(self, key, name="Зорич.pdf"):
        return signing.dumps({"key": key, "name": name}, salt=UPLOAD_SALT)

    def create(self, **extra):
        return self.client.post(reverse("book_new"), {"title": "Механика", **extra})

    def test_uploaded_file_becomes_a_row(self):
        key = self.put()
        self.create(uploaded=self.token(key))

        file = File.objects.get()
        self.assertEqual(file.book, Book.objects.get())
        self.assertEqual(file.name, "Зорич.pdf")
        self.assertEqual(file.file.name, key)  # ключ не перезаписан, файл заново не льётся
        self.assertEqual(file.size, len(b"pdf-payload"))
        self.assertEqual(file.uploader, self.author)

    def test_tampered_token_is_ignored(self):
        good = self.put(key="uploads/ok/Целый.pdf")
        broken = self.put(key="uploads/bad/Подделка.pdf")
        self.create(uploaded=[self.token(good, name="Целый.pdf"), self.token(broken) + "x"])
        self.assertEqual([f.name for f in File.objects.all()], ["Целый.pdf"])

    def test_token_for_a_missing_object_is_ignored(self):
        self.create(uploaded=self.token("uploads/abc/пусто.pdf"))
        self.assertFalse(File.objects.exists())

    def test_oversized_object_is_dropped(self):
        key = self.put(body=b"x" * 100)
        with mock.patch("attachments.uploads.MAX_DIRECT_SIZE", 10):
            self.create(uploaded=self.token(key))
        self.assertFalse(File.objects.exists())
        self.assertFalse(file_storage().exists(key))  # и сам блоб не оставляем

    def test_order_follows_the_rows_on_screen(self):
        book = Book.objects.create(title="Фихтенгольц", approved=True, uploader=self.author)
        first, second, third = [
            File.objects.create(book=book, name=f"Том {n}.pdf", order=n - 1,
                                file=ContentFile(b"x", name=f"tom{n}.pdf"))
            for n in (1, 2, 3)
        ]

        # Перетащили третий том наверх — форма прислала pk в новом порядке.
        self.client.post(reverse("book_edit", args=[book.pk]), {
            "title": "Фихтенгольц", "order": [third.pk, first.pk, second.pk],
        })
        self.assertEqual([f.name for f in book.files.all()], ["Том 3.pdf", "Том 1.pdf", "Том 2.pdf"])

    def test_new_file_lands_after_the_reordered_ones(self):
        book = Book.objects.create(title="Книга", approved=True, uploader=self.author)
        old = File.objects.create(book=book, name="Старый.pdf", order=0,
                                  file=ContentFile(b"x", name="old.pdf"))
        key = self.put(key="uploads/new/Новый.pdf")

        self.client.post(reverse("book_edit", args=[book.pk]), {
            "title": "Книга", "order": [old.pk], "uploaded": self.token(key, name="Новый.pdf"),
        })
        self.assertEqual([f.name for f in book.files.all()], ["Старый.pdf", "Новый.pdf"])

    def test_uploaded_file_can_be_renamed_on_the_way(self):
        key = self.put()
        self.create(uploaded=self.token(key), **{"uploaded-name": "Зорич. Том 1"})
        self.assertEqual(File.objects.get().name, "Зорич. Том 1")

    def test_blank_name_falls_back_to_the_original(self):
        key = self.put()
        self.create(uploaded=self.token(key), **{"uploaded-name": "   "})
        self.assertEqual(File.objects.get().name, "Зорич.pdf")

    def test_token_survives_a_form_error(self):
        key = self.put()
        response = self.client.post(reverse("book_new"), {"title": "", "uploaded": self.token(key)})
        # Книга не сохранилась, но файл уже в хранилище — токен возвращается в форму.
        self.assertFalse(Book.objects.exists())
        self.assertContains(response, self.token(key))
        self.assertContains(response, "загружено")

    def test_cleanup_takes_only_forgotten_uploads(self):
        attached = self.put(key="uploads/live/Книга.pdf")
        self.create(uploaded=self.token(attached, name="Книга.pdf"))
        forgotten = self.put(key="uploads/dead/Забытый.pdf")
        fresh = self.put(key="uploads/fresh/Свежий.pdf")

        storage = file_storage()
        for key in (attached, forgotten):
            os.utime(storage.path(key), (0, 0))  # состарили: свежие не трогаем принципиально

        call_command("clean_uploads", "--apply", verbosity=0)
        self.assertTrue(storage.exists(attached))  # к нему привязана запись
        self.assertTrue(storage.exists(fresh))  # загружен только что, форма ещё может дойти
        self.assertFalse(storage.exists(forgotten))

    def test_order_continues_after_files_sent_through_the_app(self):
        key = self.put()
        self.create(uploaded=self.token(key))
        book = Book.objects.get()

        second = self.put(key="uploads/def/Второй.pdf")
        self.client.post(reverse("book_edit", args=[book.pk]), {
            "title": "Механика", "uploaded": self.token(second, name="Второй.pdf"),
        })
        self.assertEqual([f.order for f in book.files.all()], [0, 1])
