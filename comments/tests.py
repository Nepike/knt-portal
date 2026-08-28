from unittest import mock

from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from attachments.media import media_url
from core.models import Subject
from lectorium.models import Lecture, Playlist
from materials.models import Material
from materials.tests import make_image, make_user
from users.models import User

from .models import Comment
from .views import thread


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
        return self.client.post(reverse("comment_add", args=["material", self.material.pk]), data)

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
        roots = thread(self.reader, self.material)
        self.assertEqual(len(roots), 1)
        answers = roots[0].answers
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

    def test_the_edit_form_shows_the_attached_image(self):
        # Поле файла всегда пустое, и по нему не понять, прицеплено что-то или нет.
        self.add(image=make_image())
        comment = Comment.objects.get()

        form = self.client.get(reverse("comment_edit", args=[comment.pk]))
        self.assertContains(form, comment.image.url)

    def test_the_image_can_be_taken_off_without_deleting_the_comment(self):
        # Раньше ради этого приходилось удалять комментарий и писать заново.
        self.add(text="было с картинкой", image=make_image())
        comment = Comment.objects.get()
        name, storage = comment.image.name, comment.image.storage

        self.client.post(reverse("comment_edit", args=[comment.pk]),
                         {"text": "стало без", "image-clear": "on"})

        comment.refresh_from_db()
        self.assertFalse(comment.image)
        self.assertEqual(comment.text, "стало без")
        self.assertFalse(storage.exists(name), "снятая картинка осталась в хранилище")

    def test_vote_keeps_the_replies_on_screen(self):
        # Карточка возвращается из ленты, а не собирается заново: иначе у корня
        # не оказывалось бы ответов и они пропадали от простого лайка.
        self.add(text="корень")
        root = Comment.objects.get()
        self.add(text="ответ", parent=root.pk)

        response = self.client.post(reverse("comment_like", args=[root.pk]))

        self.assertContains(response, "ответ")

    def test_huge_comment_image_is_refused(self):
        with mock.patch("attachments.uploads.MAX_IMAGE_SIZE", 10):
            response = self.add(image=make_image())

        self.assertFalse(Comment.objects.exists())
        self.assertContains(response, "больше")

    def test_failed_reply_stays_in_its_own_branch(self):
        self.add(text="корень")
        root = Comment.objects.get()

        response = self.add(text="   ", parent=root.pk)

        # Ошибка приезжает прицепленной к своей ветке, а не в композер наверху —
        # иначе тот же текст подставился бы во все формы ответа разом.
        self.assertContains(response, "Пустой комментарий")
        self.assertContains(response, "replying: true")

    def test_comment_of_a_hidden_material_is_out_of_reach(self):
        draft = Material.objects.create(
            title="Черновик", subject=self.subject, year=2025,
            status=Material.Status.PENDING, uploader=self.author,
        )
        comment = Comment.objects.create(material=draft, author=self.author, text="тайна")

        self.assertEqual(self.client.post(reverse("comment_like", args=[comment.pk])).status_code, 404)

    def test_vote_keeps_the_addressee_label(self):
        self.add(text="корень")
        root = Comment.objects.get()
        self.add(text="ответ", parent=root.pk)
        reply = Comment.objects.get(text="ответ")
        self.add(text="глубокий", parent=reply.pk)
        deep = Comment.objects.get(text="глубокий")

        response = self.client.post(reverse("comment_like", args=[deep.pk]))

        self.assertContains(response, f"#comment-{reply.pk}")

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


class CarryOverTests(TransactionTestCase):
    """Перенос обсуждений из `materials` — тот самый шаг, который на боевой базе
    выполняется ровно один раз, и проверить его задним числом будет уже нечем.

    Откатываем базу к состоянию до переезда, кладём данные СТАРОЙ моделью и катим вперёд.
    """

    BEFORE = [("materials", "0002_initial")]

    def setUp(self):
        self.author = make_user("mig-a@t.local")
        self.fan = make_user("mig-f@t.local")
        subject = Subject.objects.create(name="Оптика", dative="оптике", accusative="оптику")
        self.material = Material.objects.create(
            title="Линзы", subject=subject, year=2025,
            status=Material.Status.APPROVED, uploader=self.author,
        )

        # Возврат базы вперёд вешаем ДО отката: сорвись что-нибудь ниже, и без этого
        # откаченная база досталась бы всем следующим тестам разом.
        self.addCleanup(self.forward)
        executor = MigrationExecutor(connection)
        executor.migrate(self.BEFORE)
        self.old = executor.loader.project_state(self.BEFORE).apps.get_model("materials", "Comment")

    def forward(self):
        """Вернуть базу в нынешний вид: следующим тестам она нужна такой."""
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_the_whole_discussion_arrives_intact(self):
        root = self.old.objects.create(material_id=self.material.pk, author_id=self.author.pk, text="корень")
        reply = self.old.objects.create(material_id=self.material.pk, author_id=self.fan.pk,
                                        text="ответ", parent_id=root.pk)
        deep = self.old.objects.create(material_id=self.material.pk, author_id=self.author.pk,
                                       text="ответ на ответ", parent_id=reply.pk)
        shy = self.old.objects.create(material_id=self.material.pk, author_id=self.fan.pk,
                                      text="тихо", hide_author=True, image="comments/картинка.png")
        root.liked_users.add(self.fan.pk)
        reply.disliked_users.add(self.author.pk)

        self.forward()

        moved = {one.pk: one for one in Comment.objects.all()}
        self.assertEqual(len(moved), 4)
        # Номера сохранены: на них смотрит и `parent`, и ключи уже выплаченных наград.
        self.assertEqual(moved[root.pk].text, "корень")
        self.assertEqual(moved[deep.pk].parent_id, reply.pk)
        self.assertEqual(moved[reply.pk].parent_id, root.pk)
        self.assertEqual(moved[shy.pk].image.name, "comments/картинка.png")
        self.assertTrue(moved[shy.pk].hide_author)
        # Владелец на месте, и он ровно один.
        self.assertEqual(moved[root.pk].material_id, self.material.pk)
        self.assertIsNone(moved[root.pk].lecture_id)

    def test_votes_arrive_too(self):
        root = self.old.objects.create(material_id=self.material.pk, author_id=self.author.pk, text="корень")
        root.liked_users.add(self.fan.pk)
        root.disliked_users.add(self.author.pk)

        self.forward()

        moved = Comment.objects.get(pk=root.pk)
        self.assertEqual([u.pk for u in moved.liked_users.all()], [self.fan.pk])
        self.assertEqual([u.pk for u in moved.disliked_users.all()], [self.author.pk])

    def test_the_next_comment_does_not_collide_with_a_carried_number(self):
        """Номера проставлены руками, а счётчик таблицы об этом не знает — без сброса
        следующая же вставка упала бы на занятом номере."""
        carried = self.old.objects.create(material_id=self.material.pk, author_id=self.author.pk, text="перенесённый")

        self.forward()

        fresh = Comment.objects.create(material=self.material, author=self.author, text="новый")
        self.assertGreater(fresh.pk, carried.pk)

    def test_the_right_to_edit_other_peoples_comments_moves_along(self):
        """У нового приложения свой app_label: выданное на materials.change_comment
        перестало бы что-либо значить, и модератор молча лишился бы возможности
        чистить чужой мусор."""
        # На чистой базе прав старой модели уже нет — их заводит post_migrate по тому
        # набору моделей, который остался. Воспроизводим боевое состояние руками.
        kind = ContentType.objects.create(app_label="materials", model="comment")
        was = Permission.objects.create(
            content_type=kind, codename="change_comment", name="Can change комментарий")
        self.fan.user_permissions.add(was)

        self.forward()

        fresh = User.objects.get(pk=self.fan.pk)  # права кешируются на объекте
        self.assertTrue(fresh.has_perm("comments.change_comment"))


class LectureCommentTests(TestCase):
    """Та же лента, но под записью лекции. Приложение одно на обоих владельцев,
    и различаться должно ровно одно — за что зацеплено обсуждение."""

    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("lect-a@t.local")
        cls.reader = make_user("lect-r@t.local")
        subject = Subject.objects.create(name="Оптика", dative="оптике", accusative="оптику")
        cls.playlist = Playlist.objects.create(
            title="Оптика", subject=subject, uploader=cls.author, status=Playlist.Status.APPROVED)
        cls.lecture = Lecture.objects.create(
            playlist=cls.playlist, title="Линзы", order=0, prefix="lectures/optics-0", duration=3600)

    def setUp(self):
        self.client.force_login(self.reader)

    def add(self, **data):
        return self.client.post(reverse("comment_add", args=["lecture", self.lecture.pk]), data)

    def test_a_comment_lands_on_the_record_not_on_the_course(self):
        self.add(text="а что за формула на 12-й минуте?")

        comment = Comment.objects.get()
        self.assertEqual(comment.lecture, self.lecture)
        self.assertIsNone(comment.material_id)

    def test_the_discussion_is_shown_on_the_course_page(self):
        self.add(text="спасибо за запись")

        page = self.client.get(self.playlist.get_absolute_url())

        self.assertContains(page, "спасибо за запись")
        self.assertContains(page, "Обсуждение")

    def test_replies_work_the_same_way(self):
        self.add(text="корень")
        root = Comment.objects.get()

        self.add(text="ответ", parent=root.pk)

        roots = thread(self.reader, self.lecture)
        self.assertEqual([c.text for c in roots[0].answers], ["ответ"])

    def test_a_record_of_an_unchecked_course_is_out_of_reach(self):
        """Курс на проверке видят только автор и модерация — обсуждение тоже."""
        hidden = Playlist.objects.create(
            title="Черновик", subject=self.playlist.subject, uploader=self.author)
        lecture = Lecture.objects.create(
            playlist=hidden, title="Тайна", order=0, prefix="lectures/hidden-0")

        answer = self.client.post(reverse("comment_add", args=["lecture", lecture.pk]), {"text": "ой"})

        self.assertEqual(answer.status_code, 404)
        self.assertFalse(Comment.objects.exists())

    def test_an_unknown_kind_of_owner_is_not_found(self):
        self.assertEqual(self.client.post(reverse("comment_add", args=["book", 1]), {"text": "ой"}).status_code, 404)
