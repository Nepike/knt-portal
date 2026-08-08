from unittest import mock

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from attachments.media import file_url
from attachments.models import File
from core.models import Subject, Term
from users.models import User

from .models import Book
from .views import PAGE_SIZE


def make_user(email):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


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
    def book(cls, title, authors, subject, term, status=Book.Status.APPROVED, uploader=None):
        book = Book.objects.create(
            title=title, authors=authors, status=status, uploader=uploader or cls.author,
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
        draft = self.book("Черновик", "Некто", self.matan, self.first, status=Book.Status.PENDING)
        self.assertNotContains(self.get(), "Черновик")

        self.client.force_login(self.author)
        response = self.get()
        self.assertContains(response, "Черновик")
        self.assertContains(response, "на проверке")

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

    def htmx(self, **params):
        return self.client.get(reverse("book_list"), params, headers={"HX-Request": "true"})

    def test_filters_go_into_the_address(self):
        response = self.htmx(q="зорич", subject=self.matan.pk, term="", sort="new")
        pushed = response["HX-Push-Url"]
        self.assertIn("q=%D0%B7%D0%BE%D1%80%D0%B8%D1%87", pushed)
        self.assertIn(f"subject={self.matan.pk}", pushed)
        self.assertIn("sort=new", pushed)
        self.assertNotIn("term=", pushed)  # пустое в адрес не тащим

    def test_empty_filters_give_a_clean_address(self):
        self.assertEqual(self.htmx(q="", sort="")["HX-Push-Url"], reverse("book_list"))

    def test_next_portion_leaves_the_address_alone(self):
        # Иначе каждая догруженная порция — своя запись в истории.
        self.assertNotIn("HX-Push-Url", self.htmx(q="зорич", page=2).headers)


class SortingTests(TestCase):
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
        book = Book.objects.create(title=title, status=Book.Status.APPROVED)
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
            Book.objects.create(title=f"Книга {n:02}", status=Book.Status.APPROVED)

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


class BookDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.author = make_user("a@t.local")

    def make(self, files=1, status=Book.Status.APPROVED):
        book = Book.objects.create(title="Книга", status=status, uploader=self.author, hide_uploader=False)
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
        book = self.make(status=Book.Status.PENDING)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("book_detail", args=[book.pk])).status_code, 404)

        self.client.force_login(self.author)
        self.assertEqual(self.client.get(reverse("book_detail", args=[book.pk])).status_code, 200)


class FileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.author = make_user("a@t.local")

    def make_file(self, name="скан.pdf", stored="скан.pdf", status=Book.Status.APPROVED):
        book = Book.objects.create(title="Книга", status=status, uploader=self.author)
        return File.objects.create(
            book=book, name=name, uploader=self.author,
            file=SimpleUploadedFile(stored, b"%PDF-1.4 test"),
        )

    def test_download_counts_and_hands_the_file_to_storage(self):
        file = self.make_file()
        self.client.force_login(self.reader)
        response = self.client.get(file_url(file))

        self.assertEqual(response.status_code, 302)  # MEDIA_ACCEL выключен, значит редирект
        self.assertIn(file.file.url, response["Location"])
        file.refresh_from_db()
        self.assertEqual(file.downloads, 1)

    def test_reading_a_pdf_in_pieces_counts_as_one_download(self):
        # Просмотрщик pdf дочитывает книгу диапазонами по тому же адресу.
        file = self.make_file()
        self.client.get(file_url(file))
        self.client.get(file_url(file), headers={"range": "bytes=1024-2047"})

        file.refresh_from_db()
        self.assertEqual(file.downloads, 1)

    def test_file_opens_without_a_session(self):
        # Файлы раздаёт другой домен, куки сессии туда не приходят: разрешение даёт подпись.
        self.assertEqual(self.client.get(file_url(self.make_file())).status_code, 302)

    def test_only_our_signature_opens_a_file(self):
        self.make_file()
        bad = reverse("file_download", args=["подделка", "скан.pdf"])
        self.assertEqual(self.client.get(bad).status_code, 404)

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


def upload(name="Зорич том 1.pdf", size=64):
    return SimpleUploadedFile(name, b"x" * size)


class BookEditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")
        cls.stranger = make_user("s@t.local")
        cls.moderator = make_user("m@t.local")
        cls.moderator.user_permissions.add(
            Permission.objects.get(codename="change_book", content_type__app_label="library")
        )
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.term = Term.objects.create(number=1)

    def fields(self, **extra):
        data = {"title": "Механика", "authors": "Ландау Л. Д.", "year": "2004",
                "subjects": [self.subject.pk], "terms": [self.term.pk]}
        return {**data, **extra}

    def create(self, user, **extra):
        self.client.force_login(user)
        extra.setdefault("files", [upload()])  # книга без файлов теперь не сохраняется
        return self.client.post(reverse("book_new"), self.fields(**extra))

    def test_student_adds_a_book_and_it_waits_for_moderation(self):
        response = self.create(self.author, files=[upload()])
        book = Book.objects.get(title="Механика")
        self.assertRedirects(response, reverse("book_detail", args=[book.pk]))
        self.assertEqual(book.uploader, self.author)
        self.assertTrue(book.is_pending)
        self.assertEqual(book.year, 2004)
        self.assertEqual(list(book.subjects.all()), [self.subject])

        # Чужой её не видит, пока не проверили.
        self.client.force_login(self.stranger)
        self.assertNotContains(self.client.get(reverse("book_list")), "Механика")

    def test_moderator_publishes_at_once(self):
        self.create(self.moderator)
        self.assertTrue(Book.objects.get(title="Механика").is_published)

    def test_uploaded_files_keep_their_names(self):
        self.create(self.author, files=[upload("Ландау том 1.pdf"), upload("Задачи.djvu")])
        book = Book.objects.get(title="Механика")
        self.assertEqual([f.name for f in book.files.all()], ["Ландау том 1.pdf", "Задачи.djvu"])
        self.assertEqual([f.order for f in book.files.all()], [0, 1])
        self.assertEqual(book.files.first().uploader, self.author)

    def test_new_files_can_be_named_at_upload(self):
        self.create(self.author, files=[upload("scan001.pdf"), upload("scan002.pdf")],
                    **{"files-name": ["Ландау. Том 1", ""]})
        book = Book.objects.get(title="Механика")
        # пустое имя — остаётся имя файла
        self.assertEqual([f.name for f in book.files.all()], ["Ландау. Том 1", "scan002.pdf"])

    def test_dangerous_file_is_refused(self):
        response = self.create(self.author, files=[upload("страница.html")])
        self.assertContains(response, "загружать нельзя")
        self.assertFalse(Book.objects.exists())  # книга не создаётся, пока файл не исправят

    def test_too_big_file_is_refused(self):
        with mock.patch("attachments.uploads.MAX_FILE_SIZE", 10):
            response = self.create(self.author, files=[upload(size=100)])
        self.assertContains(response, "больше 10 Б")
        self.assertFalse(Book.objects.exists())

    def test_book_without_files_is_refused(self):
        response = self.create(self.author, files=[])
        self.assertContains(response, "хотя бы один файл")
        self.assertFalse(Book.objects.exists())

    def test_last_file_cannot_be_deleted(self):
        self.create(self.author, files=[upload()])
        book = Book.objects.get(title="Механика")
        only = book.files.get()

        response = self.client.post(reverse("book_edit", args=[book.pk]),
                                    self.fields(**{f"delete-{only.pk}": "on"}))
        self.assertContains(response, "хотя бы один файл")
        self.assertTrue(book.files.exists())

    def test_impossible_year_is_refused(self):
        response = self.create(self.author, year="20004")
        self.assertContains(response, "опечатку")
        self.assertFalse(Book.objects.exists())

    def test_uploader_renames_and_deletes_files_and_adds_new(self):
        self.create(self.author, files=[upload("Старое имя.pdf"), upload("Лишний.pdf")])
        book = Book.objects.get(title="Механика")
        old, extra = book.files.all()

        self.client.post(reverse("book_edit", args=[book.pk]), self.fields(
            title="Механика, 5-е издание",
            **{f"name-{old.pk}": "Ландау том 1", f"delete-{extra.pk}": "on"},
            files=[upload("Добавка.pdf")],
        ))
        book.refresh_from_db()
        self.assertEqual(book.title, "Механика, 5-е издание")
        self.assertEqual([f.name for f in book.files.all()], ["Ландау том 1", "Добавка.pdf"])
        self.assertFalse(extra.file.storage.exists(extra.file.name))  # блоб удалённого файла тоже ушёл

    def test_stranger_cannot_edit_or_delete(self):
        self.create(self.author)
        book = Book.objects.get(title="Механика")

        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(reverse("book_edit", args=[book.pk])).status_code, 404)  # ещё и не видна

        Book.objects.filter(pk=book.pk).update(status=Book.Status.APPROVED)
        self.assertEqual(self.client.get(reverse("book_edit", args=[book.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("book_delete", args=[book.pk])).status_code, 403)
        self.assertTrue(Book.objects.filter(pk=book.pk).exists())

    def test_moderator_edits_and_sees_foreign_unapproved(self):
        self.create(self.author)
        book = Book.objects.get(title="Механика")

        self.client.force_login(self.moderator)
        self.assertContains(self.client.get(reverse("book_list")), "Механика")
        self.assertEqual(self.client.get(reverse("book_edit", args=[book.pk])).status_code, 200)

    def test_delete_takes_the_files_with_it(self):
        self.create(self.author, files=[upload()])
        book = Book.objects.get(title="Механика")
        blob = book.files.first().file

        response = self.client.post(reverse("book_delete", args=[book.pk]))
        self.assertRedirects(response, reverse("book_list"))
        self.assertFalse(Book.objects.exists())
        self.assertFalse(File.objects.exists())
        self.assertFalse(blob.storage.exists(blob.name))

    def test_htmx_submit_gets_the_form_back_with_errors(self):
        self.client.force_login(self.author)
        response = self.client.post(reverse("book_new"), self.fields(year="20004"), headers={"HX-Request": "true"})
        self.assertContains(response, "опечатку")
        self.assertNotContains(response, "<html")  # меняем только форму, не страницу

    def test_htmx_success_sends_the_browser_to_the_book(self):
        self.client.force_login(self.author)
        response = self.client.post(reverse("book_new"), self.fields(files=[upload()]),
                                    headers={"HX-Request": "true"})
        book = Book.objects.get(title="Механика")
        # редирект htmx не отработает — HtmxRedirectMiddleware отдаёт заголовок
        self.assertEqual(response["HX-Redirect"], reverse("book_detail", args=[book.pk]))

    def test_delete_is_post_only(self):
        self.create(self.author)
        book = Book.objects.get(title="Механика")
        self.assertEqual(self.client.get(reverse("book_delete", args=[book.pk])).status_code, 405)
