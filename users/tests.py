from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from PIL import Image as PilImage

from attachments.media import media_url

from .models import User


def make_user(email="u@t.local", **extra):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345",
        must_change_password=False, **extra,
    )


def make_image(name="avatar.png"):
    buffer = BytesIO()
    PilImage.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class ProfileTests(TestCase):
    """Страница профиля рисуется целиком: ошибку в шаблоне видно только рендером."""

    @classmethod
    def setUpTestData(cls):
        cls.person = make_user("p@t.local")
        cls.viewer = make_user("v@t.local")

    def setUp(self):
        self.client.force_login(self.viewer)

    def url(self, person=None):
        return reverse("profile", args=[(person or self.person).pk])

    def test_profile_renders(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Иванов")

    def test_photo_goes_through_our_address(self):
        # Не field.url: прямая ссылка на R2 идёт через Cloudflare и до России не доезжает.
        self.person.photo = make_image()
        self.person.save(update_fields=["photo"])

        response = self.client.get(self.url())
        self.assertContains(response, media_url(self.person.photo))

    def test_initials_stand_in_for_a_missing_photo(self):
        self.assertContains(self.client.get(self.url()), "ИИ")

    def test_inactive_person_is_not_shown(self):
        gone = make_user("gone@t.local", is_active=False)
        self.assertEqual(self.client.get(self.url(gone)).status_code, 404)

    def test_own_profile_has_no_write_button(self):
        response = self.client.get(self.url(self.viewer))
        self.assertNotContains(response, "Написать сообщение")
