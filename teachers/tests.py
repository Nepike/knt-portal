from io import BytesIO

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
