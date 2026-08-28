from django.test import TestCase
from django.urls import reverse

from core.models import Subject
from lectorium.models import Playlist
from library.models import Book
from materials.models import Material
from teachers.models import Teacher
from users.models import User

from .models import Bookmark


def make_user(email="u@t.local", surname="Иванов", **extra):
    return User.objects.create_user(
        email=email, name="Иван", surname=surname, password="pass12345",
        must_change_password=False, **extra,
    )


class BookmarksBase(TestCase):
    """По одной вещи каждого вида — их четыре, и все четыре помечаются одинаково."""

    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.author = make_user("a@t.local", surname="Петров")
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.material = Material.objects.create(
            title="Конспект", subject=cls.subject, uploader=cls.author,
            status=Material.Status.APPROVED,
        )
        cls.book = Book.objects.create(
            title="Иродов", authors="Иродов И. Е.", uploader=cls.author, status=Book.Status.APPROVED,
        )
        cls.playlist = Playlist.objects.create(
            title="Механика", subject=cls.subject, uploader=cls.author,
            status=Playlist.Status.APPROVED,
        )
        cls.teacher = Teacher.objects.create(name="Пётр", surname="Сорокоумов")

    def setUp(self):
        self.client.force_login(self.reader)

    def toggle(self, kind, item):
        return self.client.post(reverse("bookmark_toggle", args=[kind, item.pk]))


class BookmarkTests(BookmarksBase):
    """Помечается ВЕЩЬ, а не адрес: удалили материал — исчезла и закладка, переименовали —
    в списке новое имя."""

    def test_every_kind_can_be_marked_and_unmarked(self):
        pairs = [
            ("material", self.material), ("book", self.book),
            ("playlist", self.playlist), ("teacher", self.teacher),
        ]
        for kind, item in pairs:
            self.assertEqual(self.toggle(kind, item).status_code, 200, kind)
            self.assertTrue(Bookmark.objects.filter(user=self.reader, **{kind: item}).exists(), kind)

        self.assertEqual(Bookmark.objects.filter(user=self.reader).count(), 4)

        for kind, item in pairs:
            self.toggle(kind, item)  # повторное нажатие снимает — другой кнопки нет
        self.assertEqual(Bookmark.objects.filter(user=self.reader).count(), 0)

    def test_the_answer_carries_the_fresh_state(self):
        """Ответ — сама кнопка: htmx подменяет её на месте, и вид должен совпасть с базой."""
        marked = self.toggle("material", self.material).content.decode()
        unmarked = self.toggle("material", self.material).content.decode()

        self.assertIn("text-accent", marked)
        self.assertNotIn("text-accent", unmarked)

    def test_a_page_shows_the_button_in_the_state_it_is_in(self):
        page = self.client.get(self.material.get_absolute_url()).content.decode()
        self.assertIn(reverse("bookmark_toggle", args=["material", self.material.pk]), page)
        self.assertNotIn("text-accent", page.split("</header>")[0])

        self.toggle("material", self.material)
        head = self.client.get(self.material.get_absolute_url()).content.decode().split("</header>")[0]
        self.assertIn("text-accent", head)

    def test_the_button_is_on_every_kind_of_page(self):
        pairs = [
            ("material", self.material), ("book", self.book),
            ("playlist", self.playlist), ("teacher", self.teacher),
        ]
        for kind, item in pairs:
            page = self.client.get(item.get_absolute_url()).content.decode()
            self.assertIn(reverse("bookmark_toggle", args=[kind, item.pk]), page, kind)

    def test_a_list_page_has_no_button(self):
        """На списке помечать нечего, и кнопка-обманка в шапке была бы шумом."""
        head = self.client.get(reverse("material_list")).content.decode().split("</header>")[0]

        self.assertNotIn("/bookmarks/material/", head)

    def test_someone_elses_draft_cannot_be_marked(self):
        """Иначе по прямому адресу чужой черновик попал бы в свой список — вместе
        с названием, которое видеть не положено."""
        draft = Material.objects.create(
            title="Черновик", subject=self.subject, uploader=self.author,
            status=Material.Status.PENDING,
        )

        self.assertEqual(self.toggle("material", draft).status_code, 404)
        self.assertFalse(Bookmark.objects.exists())

    def test_an_unknown_kind_is_not_found(self):
        self.assertEqual(self.client.post("/bookmarks/comment/1/").status_code, 404)

    def test_the_button_is_not_pressed_by_a_link(self):
        """Только POST: ссылку на закладку иначе поставил бы любой предзагрузчик."""
        self.assertEqual(self.client.get(reverse("bookmark_toggle", args=["material", 1])).status_code, 405)

    def test_two_people_mark_the_same_thing_apart(self):
        self.toggle("material", self.material)
        self.client.force_login(self.author)
        self.toggle("material", self.material)

        self.assertEqual(Bookmark.objects.filter(material=self.material).count(), 2)

    def test_deleting_the_thing_takes_the_bookmark_with_it(self):
        """Ключ на настоящую запись, а не строка с адресом: иначе в списке осталась бы
        ссылка в 404."""
        self.toggle("material", self.material)

        self.material.delete()

        self.assertFalse(Bookmark.objects.exists())


class BookmarkPageTests(BookmarksBase):
    def test_an_empty_page_says_what_the_button_is_for(self):
        page = self.client.get(reverse("bookmark_list"))

        self.assertContains(page, "Пока пусто")

    def test_things_are_grouped_by_kind(self):
        for kind, item in (("material", self.material), ("book", self.book), ("teacher", self.teacher)):
            self.toggle(kind, item)

        page = self.client.get(reverse("bookmark_list")).content.decode()

        self.assertIn("Материалы", page)
        self.assertIn("Книги", page)
        self.assertIn("Преподаватели", page)
        # Пустая группа не рисуется: заголовок над пустотой сообщает только о том,
        # что курсов не помечено, а это и так видно.
        self.assertNotIn("Курсы лекций", page)

    def test_a_row_leads_to_the_thing_and_says_what_it_is(self):
        self.toggle("book", self.book)

        page = self.client.get(reverse("bookmark_list")).content.decode()

        self.assertIn(self.book.get_absolute_url(), page)
        self.assertIn("Иродов И. Е.", page)

    def test_a_row_is_named_the_way_a_person_would_name_it(self):
        """`str()` у материала, книги и курса начинается с номера («#249: Зорич») —
        это подпись для админки. У преподавателя поля `title` нет, и там `str()` как раз
        и есть фамилия с инициалами."""
        self.toggle("book", self.book)
        self.toggle("teacher", self.teacher)

        page = self.client.get(reverse("bookmark_list")).content.decode()

        self.assertIn(">Иродов<", page)
        self.assertNotIn(f"#{self.book.pk}", page)
        self.assertIn(self.teacher.short_name(), page)

    def test_the_count_is_in_the_heading(self):
        self.toggle("material", self.material)
        self.toggle("book", self.book)

        self.assertContains(self.client.get(reverse("bookmark_list")), "2 закладки")

    def test_a_renamed_thing_shows_its_new_name(self):
        self.toggle("material", self.material)
        Material.objects.filter(pk=self.material.pk).update(title="Другое имя")

        self.assertContains(self.client.get(reverse("bookmark_list")), "Другое имя")

    def test_dropping_a_row_returns_the_list_without_it(self):
        self.toggle("material", self.material)
        self.toggle("book", self.book)
        bookmark = Bookmark.objects.get(material=self.material)

        page = self.client.post(reverse("bookmark_drop", args=[bookmark.pk])).content.decode()

        self.assertNotIn("Материалы", page)  # вместе со строкой ушла и её группа
        self.assertIn("Книги", page)
        self.assertIn("1 закладка", page)

    def test_someone_elses_bookmark_is_not_dropped(self):
        self.client.force_login(self.author)
        self.toggle("material", self.material)
        theirs = Bookmark.objects.get()

        self.client.force_login(self.reader)

        self.assertEqual(self.client.post(reverse("bookmark_drop", args=[theirs.pk])).status_code, 404)
        self.assertTrue(Bookmark.objects.filter(pk=theirs.pk).exists())

    def test_the_menu_leads_here(self):
        page = self.client.get(reverse("material_list")).content.decode()

        self.assertIn(f'href="{reverse("bookmark_list")}"', page)

    def test_the_menu_lights_the_section(self):
        self.assertEqual(self.client.get(reverse("bookmark_list")).context["section"], "bookmarks")
