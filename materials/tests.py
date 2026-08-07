from io import BytesIO
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from PIL import Image as PilImage

from attachments.media import media_url
from attachments.models import File, Image
from core.models import Subject, Term
from teachers.models import Teacher
from users.models import User

from .models import Comment, Material
from .views import PAGE_SIZE, _thread


def make_user(email):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


def make_image(name="картинка.png"):
    """Настоящий PNG: ImageField проверяет содержимое, подделка из байтов не пройдёт."""
    buffer = BytesIO()
    PilImage.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class MaterialListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.author = make_user("a@t.local")
        cls.matan = Subject.objects.create(name="Матанализ", dative="матанализу", accusative="матанализ")
        cls.physics = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.first = Term.objects.create(number=1)
        cls.second = Term.objects.create(number=2)
        cls.lector = Teacher.objects.create(name="Пётр", surname="Петров")

        cls.lectures = cls.make("Лекции по матану", cls.matan, cls.first, year=2025, teacher=cls.lector)
        cls.problems = cls.make("Задачи по физике", cls.physics, cls.second, year=2023)

    @classmethod
    def make(cls, title, subject, term, year=2025, teacher=None, status=Material.Status.APPROVED, synopsis=""):
        material = Material.objects.create(
            title=title, subject=subject, year=year, status=status,
            uploader=cls.author, synopsis=synopsis,
        )
        material.terms.add(term)
        if teacher:
            material.teachers.add(teacher)
        return material

    def setUp(self):
        self.client.force_login(self.reader)

    def get(self, **params):
        return self.client.get(reverse("material_list"), params)

    def test_page_lists_published_materials(self):
        response = self.get()
        self.assertContains(response, "Лекции по матану")
        self.assertContains(response, "Задачи по физике")

    def test_unpublished_is_visible_only_to_its_author(self):
        self.make("Черновик", self.matan, self.first, status=Material.Status.PENDING)
        self.assertNotContains(self.get(), "Черновик")

        self.client.force_login(self.author)
        self.assertContains(self.get(), "Черновик")

    def test_search_matches_title_and_synopsis(self):
        self.make("Семинары", self.matan, self.first, synopsis="разбор пределов")
        self.assertContains(self.get(q="пределов"), "Семинары")
        self.assertNotContains(self.get(q="пределов"), "Задачи по физике")

    def test_filters_by_subject_term_and_teacher(self):
        by_subject = self.get(subject=self.physics.pk)
        self.assertContains(by_subject, "Задачи по физике")
        self.assertNotContains(by_subject, "Лекции по матану")

        by_term = self.get(term=self.first.pk)
        self.assertContains(by_term, "Лекции по матану")
        self.assertNotContains(by_term, "Задачи по физике")

        by_teacher = self.get(teacher=self.lector.pk)
        self.assertContains(by_teacher, "Лекции по матану")
        self.assertNotContains(by_teacher, "Задачи по физике")

    def test_garbage_filter_does_not_break(self):
        self.assertEqual(self.get(subject="нет", term="-1", teacher="ерунда").status_code, 200)

    def test_years_are_headed_and_newest_first(self):
        body = self.get().content.decode()
        self.assertLess(body.index("2025"), body.index("2023"))  # свежий год выше
        self.assertLess(body.index("Лекции по матану"), body.index("Задачи по физике"))

    def test_filters_go_into_the_address(self):
        response = self.client.get(
            reverse("material_list"), {"subject": self.matan.pk, "term": ""}, headers={"HX-Request": "true"},
        )
        pushed = response["HX-Push-Url"]
        self.assertIn(f"subject={self.matan.pk}", pushed)
        self.assertNotIn("term=", pushed)


class YearGroupingTests(TestCase):
    """Заголовок года не должен повторяться на стыке порций."""

    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        # Один год на полторы порции: вторая порция начинается тем же годом.
        for n in range(PAGE_SIZE + 5):
            Material.objects.create(
                title=f"Материал {n:02}", subject=cls.subject, year=2024,
                status=Material.Status.APPROVED,
            )

    def setUp(self):
        self.client.force_login(self.reader)

    def test_first_portion_shows_the_year_and_asks_for_more(self):
        response = self.client.get(reverse("material_list"))
        self.assertEqual(response.content.decode().count("<article"), PAGE_SIZE)
        self.assertContains(response, "intersect once")
        self.assertContains(response, "2024")

    def test_next_portion_does_not_repeat_the_year(self):
        response = self.client.get(reverse("material_list"), {"page": 2})
        body = response.content.decode()
        self.assertEqual(body.count("<article"), 5)
        self.assertNotIn("2024", body)  # год уже написан над прошлой порцией

    def test_new_year_in_the_middle_gets_its_header(self):
        Material.objects.create(
            title="Прошлогодний", subject=self.subject, year=2023, status=Material.Status.APPROVED,
        )
        body = self.client.get(reverse("material_list"), {"page": 2}).content.decode()
        self.assertIn("2023", body)


class MaterialDetailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = make_user("r@t.local")
        cls.author = make_user("a@t.local")
        cls.moderator = make_user("m@t.local")
        cls.moderator.user_permissions.add(
            Permission.objects.get(codename="change_material", content_type__app_label="materials")
        )
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")

    def make(self, status=Material.Status.APPROVED):
        material = Material.objects.create(
            title="Механика", subject=self.subject, year=2025, status=status,
            uploader=self.author, synopsis="конспект", text="первая строка\nвторая строка",
        )
        File.objects.create(
            material=material, name="Лекция 1.pdf", file=SimpleUploadedFile("lecture.pdf", b"x"),
        )
        return material

    def test_page_shows_text_and_files(self):
        material = self.make()
        self.client.force_login(self.reader)

        response = self.client.get(reverse("material_detail", args=[material.pk]))
        self.assertContains(response, "Механика")
        self.assertContains(response, "первая строка")
        self.assertContains(response, "Лекция 1")  # значок несёт расширение, из названия оно убрано
        self.assertContains(response, "PDF")

    def test_unpublished_page_is_hidden_from_strangers(self):
        material = self.make(status=Material.Status.PENDING)

        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("material_detail", args=[material.pk])).status_code, 404)

        for user in (self.author, self.moderator):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("material_detail", args=[material.pk])).status_code, 200)

    def test_rejection_reason_is_shown_to_the_author(self):
        material = self.make(status=Material.Status.PENDING)
        material.reject(self.moderator, "Не тот предмет")
        material.save(update_fields=Material.REVIEW_FIELDS)

        self.client.force_login(self.author)
        self.assertContains(self.client.get(reverse("material_detail", args=[material.pk])), "Не тот предмет")

    def test_markdown_is_rendered_and_scripts_are_dropped(self):
        material = self.make()
        material.text = "## Заголовок\n\n- пункт\n\n<script>alert(1)</script>"
        material.save(update_fields=["text"])
        self.client.force_login(self.reader)

        body = self.client.get(reverse("material_detail", args=[material.pk])).content.decode()
        self.assertIn("<h2>Заголовок</h2>", body)
        self.assertIn("<li>пункт</li>", body)
        self.assertNotIn("<script>alert(1)</script>", body)


class MaterialEditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")
        cls.stranger = make_user("s@t.local")
        cls.moderator = make_user("m@t.local")
        cls.moderator.user_permissions.add(
            Permission.objects.get(codename="change_material", content_type__app_label="materials")
        )
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")

    def fields(self, **extra):
        return {"title": "Механика", "subject": self.subject.pk, "year": 2025, **extra}

    def create(self, user, **extra):
        self.client.force_login(user)
        return self.client.post(reverse("material_new"), self.fields(**extra))

    def test_student_adds_a_material_and_it_waits_for_moderation(self):
        with mock.patch("materials.views.notify") as notify:
            self.create(self.author, text="конспект")

        material = Material.objects.get()
        self.assertTrue(material.is_pending)
        self.assertEqual(material.uploader, self.author)
        self.assertEqual(notify.call_args.args[1], "telegram/material_pending.html")

    def test_material_without_files_is_fine(self):
        # В отличие от книги: у материала есть текст, файлы не обязательны.
        self.create(self.author, text="просто текст")
        self.assertEqual(Material.objects.get().files.count(), 0)

    def test_files_are_attached(self):
        self.create(self.author, files=SimpleUploadedFile("конспект.pdf", b"x"))
        self.assertEqual(Material.objects.get().files.count(), 1)

    def test_moderator_publishes_at_once(self):
        self.create(self.moderator)
        self.assertTrue(Material.objects.get().is_published)

    def test_impossible_year_is_refused(self):
        self.create(self.author, year=1200)
        self.assertFalse(Material.objects.exists())

    def test_stranger_cannot_edit_or_delete(self):
        # Опубликованный: неопубликованного чужой вообще не видит и получил бы 404.
        self.create(self.moderator)
        material = Material.objects.get()

        self.client.force_login(self.stranger)
        self.assertEqual(self.client.post(reverse("material_edit", args=[material.pk]), self.fields()).status_code, 403)
        self.assertEqual(self.client.post(reverse("material_delete", args=[material.pk])).status_code, 403)

    def test_edit_by_the_author_sends_it_back_to_review(self):
        self.create(self.moderator)  # опубликован сразу
        material = Material.objects.get()
        material.uploader = self.author
        material.save(update_fields=["uploader"])

        self.client.force_login(self.author)
        self.client.post(reverse("material_edit", args=[material.pk]), self.fields(title="Другое"))

        material.refresh_from_db()
        self.assertTrue(material.is_pending)

    def test_moderation_fixing_a_rejected_material_publishes_it(self):
        self.create(self.author)
        material = Material.objects.get()
        material.reject(self.moderator, "Не тот предмет")
        material.save(update_fields=Material.REVIEW_FIELDS)

        self.client.force_login(self.moderator)
        self.client.post(reverse("material_edit", args=[material.pk]), self.fields(title="Поправлено"))

        material.refresh_from_db()
        self.assertTrue(material.is_published)
        self.assertEqual(material.review_note, "")

    def test_review_decisions(self):
        self.create(self.author)
        material = Material.objects.get()
        self.client.force_login(self.moderator)

        self.client.post(reverse("material_review", args=[material.pk]), {"decision": "approve"})
        material.refresh_from_db()
        self.assertTrue(material.is_published)
        self.assertEqual(material.reviewed_by, self.moderator)

        self.client.post(
            reverse("material_review", args=[material.pk]), {"decision": "reject", "note": "Дубликат"},
        )
        material.refresh_from_db()
        self.assertEqual(material.status, Material.Status.REJECTED)
        self.assertEqual(material.review_note, "Дубликат")

    def test_stranger_cannot_decide(self):
        self.create(self.author)
        material = Material.objects.get()

        self.client.force_login(self.stranger)
        self.assertEqual(
            self.client.post(reverse("material_review", args=[material.pk]), {"decision": "approve"}).status_code, 403,
        )

    def test_material_shows_up_in_the_moderation_queue(self):
        self.create(self.author)
        self.client.force_login(self.moderator)
        self.assertContains(self.client.get(reverse("review_queue")), "Механика")

    def test_delete_takes_the_files_with_it(self):
        self.create(self.author, files=SimpleUploadedFile("конспект.pdf", b"x"))
        material = Material.objects.get()

        with mock.patch("materials.views.notify"):
            self.client.post(reverse("material_delete", args=[material.pk]))

        self.assertFalse(Material.objects.exists())
        self.assertFalse(File.objects.exists())


class GalleryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")

    def setUp(self):
        self.client.force_login(self.author)

    def fields(self, **extra):
        return {"title": "Механика", "subject": self.subject.pk, "year": 2025, **extra}

    def test_images_are_attached_on_create(self):
        self.client.post(reverse("material_new"), self.fields(images=[make_image("a.png"), make_image("b.png")]))
        self.assertEqual(Material.objects.get().images.count(), 2)

    def test_marked_image_goes_away_and_order_follows_the_screen(self):
        self.client.post(reverse("material_new"), self.fields(images=[make_image("a.png"), make_image("b.png")]))
        material = Material.objects.get()
        first, second = material.images.all()

        # Перетащили вторую наверх и пометили первую на удаление.
        self.client.post(reverse("material_edit", args=[material.pk]), self.fields(**{
            "image-order": [second.pk], f"delete-image-{first.pk}": "on",
        }))

        self.assertEqual([i.pk for i in material.images.all()], [second.pk])
        self.assertFalse(Image.objects.filter(pk=first.pk).exists())

    def test_huge_image_is_refused(self):
        with mock.patch("attachments.uploads.MAX_IMAGE_SIZE", 10):
            response = self.client.post(reverse("material_new"), self.fields(images=make_image()))
        self.assertContains(response, "больше")
        self.assertFalse(Material.objects.exists())


class CommentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")
        cls.reader = make_user("r@t.local")
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.material = Material.objects.create(
            title="Механика", subject=cls.subject, year=2025,
            status=Material.Status.APPROVED, uploader=cls.author,
        )

    def setUp(self):
        self.client.force_login(self.reader)

    def add(self, **data):
        return self.client.post(reverse("comment_add", args=[self.material.pk]), data)

    def test_comment_appears_in_the_thread(self):
        response = self.add(text="Спасибо, выручил")

        self.assertContains(response, "Спасибо, выручил")
        self.assertEqual(Comment.objects.get().author, self.reader)

    def test_empty_comment_is_refused(self):
        response = self.add(text="   ")

        self.assertFalse(Comment.objects.exists())
        self.assertContains(response, "Пустой комментарий")

    def test_comment_text_goes_through_markdown(self):
        self.add(text="**важно** и $x^2$")

        body = self.client.get(self.material.get_absolute_url()).content.decode()
        self.assertIn("<strong>важно</strong>", body)
        self.assertIn("arithmatex", body)  # формулу дорисует KaTeX в браузере

    def test_reply_to_a_reply_stays_on_one_level_but_remembers_who(self):
        self.add(text="корень")
        root = Comment.objects.get()
        self.add(text="ответ", parent=root.pk)
        reply = Comment.objects.get(text="ответ")

        self.add(text="ответ на ответ", parent=reply.pk)

        # В базе дерево настоящее: видно, кому именно отвечали.
        deep = Comment.objects.get(text="ответ на ответ")
        self.assertEqual(deep.parent_id, reply.pk)

        # А на экране ветка плоская: оба ответа лежат под корнем, и у глубокого
        # подписан адресат — иначе он неотличим от ответа самому корню.
        thread = _thread(self.reader, self.material)
        self.assertEqual(len(thread), 1)
        answers = thread[0].answers
        self.assertEqual([c.text for c in answers], ["ответ", "ответ на ответ"])
        self.assertIsNone(answers[0].addressee)
        self.assertEqual(answers[1].addressee.pk, reply.pk)

    def test_addressee_name_is_shown_in_the_thread(self):
        self.add(text="корень")
        root = Comment.objects.get()
        self.client.force_login(self.author)
        self.add(text="ответ", parent=root.pk)
        reply = Comment.objects.get(text="ответ")
        self.client.force_login(self.reader)
        self.add(text="ответ на ответ", parent=reply.pk)

        body = self.client.get(self.material.get_absolute_url()).content.decode()
        self.assertIn(f"#comment-{reply.pk}", body)  # ссылка на того, кому отвечали

    def test_delete_refreshes_the_whole_thread(self):
        self.add(text="корень")
        root = Comment.objects.get()
        self.add(text="ответ", parent=root.pk)

        response = self.client.post(reverse("comment_delete", args=[root.pk]))

        # Не 204: вместе с корнем исчезают ответы и меняется счётчик — лента приходит целиком.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Пока тихо")

    def test_image_can_replace_the_text(self):
        self.add(image=make_image())

        self.assertTrue(Comment.objects.get().image)

    def test_vote_toggles_and_switches(self):
        self.add(text="норм")
        comment = Comment.objects.get()

        self.client.post(reverse("comment_like", args=[comment.pk]))
        self.assertEqual(comment.liked_users.count(), 1)

        self.client.post(reverse("comment_dislike", args=[comment.pk]))
        self.assertEqual(comment.liked_users.count(), 0)  # голос переехал, а не удвоился
        self.assertEqual(comment.disliked_users.count(), 1)

        self.client.post(reverse("comment_dislike", args=[comment.pk]))
        self.assertEqual(comment.disliked_users.count(), 0)  # повторный клик снимает

    def test_author_edits_and_stranger_cannot(self):
        self.add(text="было")
        comment = Comment.objects.get()

        self.client.post(reverse("comment_edit", args=[comment.pk]), {"text": "стало"})
        comment.refresh_from_db()
        self.assertEqual(comment.text, "стало")

        self.client.force_login(self.author)
        self.assertEqual(
            self.client.post(reverse("comment_edit", args=[comment.pk]), {"text": "чужое"}).status_code, 403,
        )

    def test_delete_takes_the_replies_with_it(self):
        self.add(text="корень")
        root = Comment.objects.get()
        self.add(text="ответ", parent=root.pk)

        self.client.post(reverse("comment_delete", args=[root.pk]))

        self.assertFalse(Comment.objects.exists())

    def test_image_url_survives_the_signature_expiring(self):
        # В разметку идёт наш постоянный адрес, а не подписанная на час ссылка R2:
        # иначе картинки протухали бы прямо в открытой вкладке и не кешировались.
        self.add(image=make_image())
        comment = Comment.objects.get()

        body = self.client.get(self.material.get_absolute_url()).content.decode()
        self.assertIn(media_url(comment.image), body)
        self.assertEqual(media_url(comment.image), media_url(comment.image))  # адрес постоянный

    def test_anonymous_comment_hides_the_name(self):
        self.add(text="тихо", hide_author="on")

        body = self.client.get(self.material.get_absolute_url()).content.decode()
        self.assertIn("Аноним", body)
        self.assertNotIn("Иванов", body.split("Обсуждение")[1])
