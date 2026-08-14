import logging
import re
from datetime import timedelta
from io import BytesIO, StringIO

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

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


class SessionForCommandTests(TestCase):
    """Ключ сессии из консоли: им и правда можно смотреть сайт чужими глазами."""

    LOGGER = "users.management.commands.session_for"

    def setUp(self):
        self.person = make_user("student@t.local")
        # Команда пишет предупреждение в лог — в выводе сьюта оно только шумит.
        # Что запись действительно появляется, проверяет отдельный тест ниже.
        logger = logging.getLogger(self.LOGGER)
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.CRITICAL)

    def run_command(self, *args):
        out = StringIO()
        call_command("session_for", *args, stdout=out)
        return out.getvalue()

    def key_from(self, text):
        return re.search(rf"{settings.SESSION_COOKIE_NAME} = (\S+)", text).group(1)

    def test_the_key_actually_opens_the_site_as_that_person(self):
        key = self.key_from(self.run_command(self.person.email))
        self.client.cookies[settings.SESSION_COOKIE_NAME] = key
        response = self.client.get(reverse("profile", args=[self.person.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["user"], self.person)

    def test_it_expires_soon_rather_than_living_for_weeks(self):
        """Ключ — пароль на предъявителя: попал на скриншот, значит действует до истечения."""
        self.run_command(self.person.email)
        session = Session.objects.get()
        self.assertLess(session.expire_date, timezone.now() + timedelta(hours=1))

    def test_a_shorter_life_can_be_asked_for(self):
        self.run_command(self.person.email, "--minutes", "5")
        self.assertLess(Session.objects.get().expire_date, timezone.now() + timedelta(minutes=6))

    def test_it_takes_an_id_as_well_as_an_email(self):
        self.assertIn(self.person.email, self.run_command(str(self.person.pk)))

    def test_end_closes_what_was_handed_out(self):
        self.run_command(self.person.email)
        self.run_command(self.person.email)
        self.assertEqual(Session.objects.count(), 2)
        self.run_command(self.person.email, "--end")
        self.assertEqual(Session.objects.count(), 0)

    def test_it_refuses_instead_of_guessing(self):
        with self.assertRaises(CommandError):
            self.run_command("nobody@t.local")
        gone = make_user("gone@t.local", is_active=False)
        with self.assertRaises(CommandError):
            self.run_command(gone.email)

    def test_handing_out_a_session_leaves_a_trace_in_the_log(self):
        """Единственная запись о том, что под этим человеком кто-то ходил: на самих
        действиях следа не остаётся, они выглядят как его собственные."""
        with self.assertLogs(self.LOGGER, "WARNING") as log:
            self.run_command(self.person.email)
        self.assertIn(self.person.email, log.output[0])
