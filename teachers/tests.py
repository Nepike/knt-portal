from io import BytesIO

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from PIL import Image as PilImage

from users.models import User

from .models import Review, Teacher


def make_user(email):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


def make_image(name="доска.png"):
    """Настоящий PNG: ImageField смотрит на содержимое, подделка из байтов не пройдёт."""
    buffer = BytesIO()
    PilImage.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class ReviewImageTests(TestCase):
    """Картинка в отзыве — та же механика, что и у комментария к материалу."""

    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")
        cls.teacher = Teacher.objects.create(name="Пётр", surname="Петров")

    def setUp(self):
        self.client.force_login(self.author)
        self.url = reverse("teacher_detail", args=[self.teacher.pk])

    def review_with_image(self, **extra):
        self.client.post(self.url, {"text": "Доска после пары", "image": make_image(), **extra})
        return Review.objects.get()

    def test_a_review_may_be_a_picture_alone(self):
        review = self.review_with_image(text="")
        self.assertTrue(review.image)
        self.assertTrue(review.is_detailed())  # значит виден всем и ему ставят лайки

    def test_a_picture_only_some_browsers_show_is_refused(self):
        from attachments.tests import heic, picture

        response = self.client.post(self.url, {"text": "Доска после пары", "image": picture("TIFF")})
        self.assertContains(response, "формат TIFF показывают не все браузеры")

        response = self.client.post(self.url, {"text": "Доска после пары", "image": heic()})
        self.assertContains(response, "снимок HEIC с айфона сохрани как JPEG")
        self.assertFalse(Review.objects.exists())

    def test_an_empty_review_is_still_refused(self):
        response = self.client.post(self.url, {"text": ""})
        self.assertFalse(Review.objects.exists())
        self.assertContains(response, "Поставь хотя бы одну оценку")

    def test_the_card_shows_the_picture(self):
        self.review_with_image()
        self.assertContains(self.client.get(self.url), Review.objects.get().image.url)

    def test_the_edit_form_shows_the_picture_that_is_already_attached(self):
        # Иначе при правке не понять, есть картинка или нет: поле файла всегда пустое.
        review = self.review_with_image()
        form = self.client.get(reverse("review_edit", args=[review.pk]))
        self.assertContains(form, review.image.url)

    def test_the_picture_can_be_taken_off_without_deleting_the_review(self):
        review = self.review_with_image()
        name, storage = review.image.name, review.image.storage

        self.client.post(reverse("review_edit", args=[review.pk]), {
            "text": "Доска после пары", "image-clear": "on",
        })

        review.refresh_from_db()
        self.assertFalse(review.image)
        self.assertEqual(review.text, "Доска после пары")
        self.assertFalse(storage.exists(name), "снятая картинка осталась в хранилище")

    def test_the_replaced_picture_does_not_stay_in_the_storage(self):
        review = self.review_with_image()
        old, storage = review.image.name, review.image.storage

        self.client.post(reverse("review_edit", args=[review.pk]), {
            "text": "Доска после пары", "image": make_image("другая.png"),
        })

        review.refresh_from_db()
        self.assertNotEqual(review.image.name, old)
        self.assertFalse(storage.exists(old))
        self.assertTrue(storage.exists(review.image.name))


class ReviewVoteTests(TestCase):
    """Голосуют за то, что можно прочесть. У отзыва из одних оценок кнопок голоса нет,
    и ручка отвечает тем же: лайки оплачиваются."""

    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")
        cls.voter = make_user("v@t.local")
        cls.teacher = Teacher.objects.create(name="Пётр", surname="Петров")

    def setUp(self):
        self.client.force_login(self.voter)

    def review(self, **fields):
        return Review.objects.create(teacher=self.teacher, author=self.author, **fields)

    def vote(self, review, name="review_like"):
        return self.client.post(reverse(name, args=[review.pk])).status_code

    def test_a_review_with_text_takes_a_vote(self):
        review = self.review(text="Объясняет понятно")

        self.assertEqual(self.vote(review), 200)
        self.assertEqual(list(review.liked_users.all()), [self.voter])

    def test_a_picture_alone_is_enough_to_vote_for(self):
        review = self.review(image=make_image())

        self.assertEqual(self.vote(review, "review_dislike"), 200)
        self.assertEqual(list(review.disliked_users.all()), [self.voter])

    def test_scores_alone_take_no_vote(self):
        review = self.review(score_knowledge=5)

        self.assertEqual((self.vote(review), self.vote(review, "review_dislike")), (403, 403))
        self.assertFalse(review.liked_users.exists() or review.disliked_users.exists())


class ReviewRightsTests(TestCase):
    """Чужой отзыв правит и удаляет тот, кому дано право, — а не всякий вошедший."""

    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")
        cls.stranger = make_user("s@t.local")
        cls.teacher = Teacher.objects.create(name="Пётр", surname="Петров")

    def setUp(self):
        self.review = Review.objects.create(teacher=self.teacher, author=self.author, text="Было")

    def allowed(self, email, codename):
        user = make_user(email)
        user.user_permissions.add(Permission.objects.get(content_type__app_label="teachers", codename=codename))
        return user

    def edit(self, who):
        self.client.force_login(who)
        answer = self.client.post(reverse("review_edit", args=[self.review.pk]), {"text": "Стало"})
        self.review.refresh_from_db()
        return answer.status_code, self.review.text

    def delete(self, who):
        self.client.force_login(who)
        answer = self.client.post(reverse("review_delete", args=[self.review.pk]))
        return answer.status_code, Review.objects.filter(pk=self.review.pk).exists()

    def test_a_stranger_can_neither_edit_nor_delete(self):
        self.assertEqual(self.edit(self.stranger), (403, "Было"))
        self.assertEqual(self.delete(self.stranger), (403, True))

    def test_a_stranger_does_not_get_the_edit_form_either(self):
        self.client.force_login(self.stranger)

        self.assertEqual(self.client.get(reverse("review_edit", args=[self.review.pk])).status_code, 403)

    def test_the_author_edits_and_deletes_his_own(self):
        self.assertEqual(self.edit(self.author), (200, "Стало"))
        self.assertEqual(self.delete(self.author), (200, False))

    def test_the_right_to_change_lets_one_edit_but_not_delete(self):
        editor = self.allowed("e@t.local", "change_review")

        self.assertEqual(self.edit(editor), (200, "Стало"))
        self.assertEqual(self.delete(editor), (403, True))

    def test_the_right_to_delete_lets_one_delete_but_not_edit(self):
        remover = self.allowed("d@t.local", "delete_review")

        self.assertEqual(self.edit(remover), (403, "Было"))
        self.assertEqual(self.delete(remover), (200, False))
