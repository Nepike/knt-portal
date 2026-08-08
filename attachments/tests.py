"""Прямая загрузка в хранилище: подпись ссылки и приём уже загруженного файла.

Владельцем берём книгу — она проще всех, но проверяется код attachments.
"""
import json
import os
from types import SimpleNamespace
from unittest import mock
from urllib.parse import unquote

from django.core import signing
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from library.models import Book
from users.models import User

from .media import file_url, media_url, redirect_url
from .models import File
from .storage import file_storage, random_key
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


class MediaUrlTests(SimpleTestCase):
    """Какой адрес попадает в разметку."""

    def field(self, storage=None):
        return SimpleNamespace(
            name="images/board.png", url="/media/images/board.png",
            storage=storage or FileSystemStorage(),
        )

    def test_local_disk_in_development_goes_straight_to_the_file(self):
        self.assertEqual(media_url(self.field()), "/media/images/board.png")

    def test_signing_storage_goes_through_our_redirect(self):
        self.assertTrue(media_url(self.field(storage=object())).startswith("/img/"))

    @override_settings(FILES_BASE_URL="https://files.example")
    def test_own_domain_takes_everything_including_local_disk(self):
        # Адрес один и тот же в обоих режимах — переключение хранилища ссылок не меняет.
        self.assertTrue(media_url(self.field()).startswith("https://files.example/img/"))

    def test_empty_field_gives_empty_address(self):
        self.assertEqual(media_url(None), "")


class FileUrlTests(TestCase):
    """Адрес файла: подпись вместо сессии, имя в хвосте — для браузера."""

    def make(self, name="Зорич. Том 1.pdf"):
        book = Book.objects.create(title="Книга", status=Book.Status.APPROVED, uploader=make_user())
        return File.objects.create(book=book, name=name, file=ContentFile(b"pdf", name="z.pdf"))

    def test_address_carries_the_name_and_survives_a_slash_in_it(self):
        url = file_url(self.make(name="Лекции 1/2.pdf"))
        self.assertIn("%D0%9B", url)  # имя доехало, косая заменена
        self.assertNotIn("1/2", url)

    def test_address_is_the_same_every_time(self):
        file = self.make()
        # Иначе кеш браузера был бы бесполезен: адрес ехал бы на каждом рендере.
        self.assertEqual(file_url(file), file_url(file))

    @override_settings(FILES_BASE_URL="https://files.example")
    def test_own_domain_is_prepended(self):
        self.assertTrue(file_url(self.make()).startswith("https://files.example/f/"))


@override_settings(MEDIA_ACCEL=True)
class AccelTests(TestCase):
    """На сервере байты отдаёт nginx, приложение возвращает только заголовок."""

    def setUp(self):
        self.key = file_storage().save("images/board.png", ContentFile(b"png"))

    def test_local_storage_points_nginx_at_the_media_folder(self):
        response = self.client.get(redirect_url(self.key))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Accel-Redirect"], "/__local/" + self.key)
        self.assertEqual(response.content, b"")  # байты через приложение не идут

    def test_content_type_comes_from_the_file_name(self):
        # При X-Accel-Redirect побеждает заголовок приложения, а не mime-тип nginx:
        # с дефолтным text/html браузер показал бы pdf текстом, а картинку — ничем.
        self.assertEqual(self.client.get(redirect_url(self.key))["Content-Type"], "image/png")

        key = file_storage().save("books/Зорич.pdf", ContentFile(b"%PDF"))
        self.assertEqual(self.client.get(redirect_url(key))["Content-Type"], "application/pdf")

    def test_unknown_extension_is_handed_over_as_bytes(self):
        key = file_storage().save("books/конспект.djvu", ContentFile(b"x"))
        self.assertEqual(
            self.client.get(redirect_url(key))["Content-Type"], "application/octet-stream",
        )

    def test_redirect_is_kept_when_nginx_is_not_there(self):
        with self.settings(MEDIA_ACCEL=False):
            response = self.client.get(redirect_url(self.key))
        self.assertEqual(response.status_code, 302)

    def test_cyrillic_name_is_escaped_for_nginx(self):
        key = file_storage().save("books/Зорич том 1.pdf", ContentFile(b"pdf"))
        response = self.client.get(redirect_url(key))

        target = response["X-Accel-Redirect"]
        self.assertTrue(target.startswith("/__local/books/"))
        self.assertNotIn(" ", target)  # заголовок обязан быть ASCII
        self.assertEqual(unquote(target[len("/__local/"):]), key)


class RandomKeyTests(SimpleTestCase):
    def test_key_is_unguessable_but_keeps_the_file_name(self):
        # Бакет публичный: по предсказуемому пути библиотеку перебрали бы снаружи.
        first = random_key("books", "Зорич том 1.pdf")
        second = random_key("books", "Зорич том 1.pdf")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("books/"))
        self.assertTrue(first.endswith("/Зорич том 1.pdf"))


class MediaImageTests(TestCase):
    """Отдача картинки без nginx — так работает разработка."""

    def setUp(self):
        self.key = file_storage().save("images/board.png", ContentFile(b"png"))

    def test_redirect_leads_to_the_storage_and_may_be_cached(self):
        response = self.client.get(redirect_url(self.key))

        self.assertEqual(response.status_code, 302)
        self.assertIn(self.key, response["Location"])
        # Кеш короче подписи R2: иначе браузер переиспользовал бы протухшую ссылку.
        self.assertIn("max-age", response["Cache-Control"])

    def test_picture_opens_without_a_session(self):
        # Домен файлов другой, куки туда не приходят: разрешение даёт подпись в адресе.
        self.assertEqual(self.client.get(redirect_url(self.key)).status_code, 302)

    def test_tampered_token_is_not_found(self):
        self.assertEqual(self.client.get(redirect_url(self.key) + "x/").status_code, 404)


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

    def test_tampered_token_stops_the_save(self):
        good = self.put(key="uploads/ok/Целый.pdf")
        broken = self.put(key="uploads/bad/Подделка.pdf")
        response = self.create(uploaded=[self.token(good, name="Целый.pdf"), self.token(broken) + "x"])
        self.assertContains(response, "не доехал до хранилища")
        self.assertFalse(Book.objects.exists())

    def test_missing_object_stops_the_save(self):
        # Файл не доехал — книга НЕ сохраняется молча без него.
        response = self.create(uploaded=self.token("uploads/abc/пусто.pdf"))
        self.assertContains(response, "не доехал до хранилища")
        self.assertFalse(File.objects.exists())
        self.assertFalse(Book.objects.exists())

    def test_oversized_object_stops_the_save(self):
        key = self.put(body=b"x" * 100)
        with mock.patch("attachments.uploads.MAX_DIRECT_SIZE", 10):
            response = self.create(uploaded=self.token(key))
        self.assertContains(response, "больше")
        self.assertFalse(Book.objects.exists())
        self.assertFalse(File.objects.exists())

    def test_order_follows_the_rows_on_screen(self):
        book = Book.objects.create(title="Фихтенгольц", status=Book.Status.APPROVED, uploader=self.author)
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
        book = Book.objects.create(title="Книга", status=Book.Status.APPROVED, uploader=self.author)
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
