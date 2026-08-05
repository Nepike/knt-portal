import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from attachments.models import File
from core.models import Subject, Term
from users.models import User

from .models import Book
from .views import PAGE_SIZE


def make_user(email):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


class MediaTestCase(TestCase):
    """Файлы тестов — во временный каталог, иначе они копятся в media/ проекта."""

    @classmethod
    def setUpClass(cls):
        cls._media = tempfile.TemporaryDirectory()
        cls._override = override_settings(MEDIA_ROOT=cls._media.name)
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._override.disable()
        cls._media.cleanup()


class BookListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.author = make_user("a@t.local")
        cls.matan = Subject.objects.create(name="Матанализ", dative="матанализу", accusative="матанализ")
        cls.physics = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.first = Term.objects.create(number=1)
        cls.second = Term.objects.create(number=2)

        cls.zorich = cls.book("Математический анализ", "Зорич В. А.", cls.matan, cls.first)
        cls.irodov = cls.book("Задачи по общей физике", "Иродов И. Е.", cls.physics, cls.second)

    @classmethod
    def book(cls, title, authors, subject, term, approved=True, uploader=None):
        book = Book.objects.create(
            title=title, authors=authors, approved=approved, uploader=uploader or cls.author,
        )
        book.subjects.add(subject)
        book.terms.add(term)
        return book

    def setUp(self):
        self.client.force_login(self.reader)

    def get(self, **params):
        return self.client.get(reverse("book_list"), params)

    def test_page_lists_approved_books(self):
        response = self.get()
        self.assertContains(response, "Математический анализ")
        self.assertContains(response, "Задачи по общей физике")

    def test_unapproved_is_visible_only_to_its_uploader(self):
        draft = self.book("Черновик", "Некто", self.matan, self.first, approved=False)
        self.assertNotContains(self.get(), "Черновик")

        self.client.force_login(self.author)
        response = self.get()
        self.assertContains(response, "Черновик")
        self.assertContains(response, "на модерации")

    def test_search_matches_title_and_author(self):
        self.assertContains(self.get(q="математич"), "Зорич")
        self.assertNotContains(self.get(q="математич"), "Иродов")
        self.assertContains(self.get(q="иродов"), "Иродов")  # ищем и по автору

    def test_filters_by_subject_and_term(self):
        by_subject = self.get(subject=self.physics.pk)
        self.assertContains(by_subject, "Иродов")
        self.assertNotContains(by_subject, "Зорич")

        by_term = self.get(term=self.first.pk)
        self.assertContains(by_term, "Зорич")
        self.assertNotContains(by_term, "Иродов")

    def test_garbage_filter_does_not_break(self):
        self.assertEqual(self.get(subject="нет", term="-1", sort="ерунда").status_code, 200)

    def test_htmx_gets_only_the_list(self):
        response = self.client.get(reverse("book_list"), headers={"HX-Request": "true"})
        self.assertContains(response, "Зорич")
        self.assertNotContains(response, "<html")  # без обвязки страницы


class SortingTests(MediaTestCase):
    """Порядок книг: по скачиваниям, по свежести, по алфавиту."""

    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.terms = [Term.objects.create(number=n) for n in (1, 2)]

        cls.old = cls.make("Аистов", downloads=[10, 10])
        cls.old.terms.add(*cls.terms)  # два семестра: фильтр джойнит их к двум файлам
        cls.fresh = cls.make("Яковлев", downloads=[30])
        cls.fresh.terms.add(cls.terms[0])

    @classmethod
    def make(cls, title, downloads):
        book = Book.objects.create(title=title, approved=True)
        for n in downloads:
            File.objects.create(
                book=book, name=f"{title}.pdf", downloads=n,
                file=SimpleUploadedFile(f"{title}.pdf", b"x"),
            )
        return book

    def setUp(self):
        self.client.force_login(self.reader)

    def order(self, **params):
        body = self.client.get(reverse("book_list"), params).content.decode()
        return sorted(("Аистов", "Яковлев"), key=body.index)

    def test_popular_first_by_default(self):
        self.assertEqual(self.order(), ["Яковлев", "Аистов"])  # 30 > 10 + 10

    def test_sum_survives_a_filter_over_m2m(self):
        self.assertEqual(self.order(term=self.terms[0].pk), ["Яковлев", "Аистов"])

    def test_new_and_alphabet(self):
        self.assertEqual(self.order(sort="new"), ["Яковлев", "Аистов"])
        self.assertEqual(self.order(sort="title"), ["Аистов", "Яковлев"])

    def test_unknown_sort_falls_back_to_popular(self):
        self.assertEqual(self.order(sort="; drop table"), self.order(sort="popular"))


class PaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.total = PAGE_SIZE + 5
        for n in range(cls.total):
            Book.objects.create(title=f"Книга {n:02}", approved=True)

    def setUp(self):
        self.client.force_login(self.reader)

    def get(self, **params):
        return self.client.get(reverse("book_list"), params)

    def test_first_page_is_limited_and_asks_for_more(self):
        response = self.get()
        self.assertEqual(response.content.decode().count("<article"), PAGE_SIZE)
        self.assertContains(response, "intersect once")
        self.assertContains(response, f"{self.total} книг")

    def test_last_page_has_the_rest_and_no_sentinel(self):
        response = self.get(page=2)
        self.assertEqual(response.content.decode().count("<article"), self.total - PAGE_SIZE)
        self.assertNotContains(response, "intersect once")
        self.assertNotContains(response, "книг</p>")  # счётчик только на первой порции

    def test_sentinel_keeps_filters_and_sorting(self):
        body = self.get(sort="title").content.decode()
        self.assertIn("sort=title", body)
        self.assertIn("page=2", body)


class BookDetailTests(MediaTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.author = make_user("a@t.local")

    def make(self, files=1, approved=True):
        book = Book.objects.create(title="Книга", approved=approved, uploader=self.author, hide_uploader=False)
        for n in range(files):
            File.objects.create(
                book=book, name=f"Том {n}.pdf", order=n,
                file=SimpleUploadedFile(f"tom{n}.pdf", b"x"),
            )
        return book

    def test_page_shows_every_file(self):
        book = self.make(files=5)
        self.client.force_login(self.reader)
        response = self.client.get(reverse("book_detail", args=[book.pk]))
        self.assertContains(response, "5 файлов")
        for n in range(5):
            self.assertContains(response, f"Том {n}")

    def test_card_shows_only_the_first_files(self):
        self.make(files=5)
        self.client.force_login(self.reader)
        body = self.client.get(reverse("book_list")).content.decode()
        self.assertIn("Том 2", body)
        self.assertNotIn("Том 3", body)
        self.assertIn("Ещё 2 файла", body)

    def test_unapproved_page_is_hidden_from_strangers(self):
        book = self.make(approved=False)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("book_detail", args=[book.pk])).status_code, 404)

        self.client.force_login(self.author)
        self.assertEqual(self.client.get(reverse("book_detail", args=[book.pk])).status_code, 200)


class FileTests(MediaTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.author = make_user("a@t.local")

    def make_file(self, name="скан.pdf", stored="скан.pdf", approved=True):
        book = Book.objects.create(title="Книга", approved=approved, uploader=self.author)
        return File.objects.create(
            book=book, name=name, uploader=self.author,
            file=SimpleUploadedFile(stored, b"%PDF-1.4 test"),
        )

    def test_download_counts_and_redirects_to_storage(self):
        file = self.make_file()
        self.client.force_login(self.reader)
        response = self.client.get(reverse("file_download", args=[file.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(file.file.url, response["Location"])
        file.refresh_from_db()
        self.assertEqual(file.downloads, 1)

    def test_file_of_unapproved_book_is_hidden_from_strangers(self):
        file = self.make_file(approved=False)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("file_download", args=[file.pk])).status_code, 404)

        self.client.force_login(self.author)
        self.assertEqual(self.client.get(reverse("file_download", args=[file.pk])).status_code, 302)

    def test_size_is_filled_on_save(self):
        file = self.make_file()
        self.assertEqual(file.size, len(b"%PDF-1.4 test"))
        self.assertTrue(file.human_size())

    def test_extension_comes_from_the_file_not_from_the_name(self):
        file = self.make_file(name="Конспект", stored="конспект.djvu")
        self.assertEqual(file.extension, "djvu")
        self.assertEqual(file.kind, "pdf")  # книжные сканы красим как pdf
        self.assertEqual(file.label, "Конспект")

    def test_label_drops_the_duplicated_extension(self):
        self.assertEqual(self.make_file(name="Зорич том 1.pdf").label, "Зорич том 1")

    def test_unknown_extension_is_still_shown(self):
        file = self.make_file(name="архив.7z", stored="архив.7z")
        self.assertEqual(file.kind, "archive")
        self.assertEqual(self.make_file(stored="файл").kind, "other")

    def test_extension_badge_is_rendered_in_the_card(self):
        self.make_file(name="Зорич том 1.pdf")
        self.client.force_login(self.reader)
        self.assertContains(self.client.get(reverse("book_list")), "PDF")
