import logging
import re
import time
from base64 import b64encode
from datetime import timedelta
from io import BytesIO, StringIO

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from PIL import Image as PilImage

from attachments.media import media_url
from core.models import Subject, Term
from materials.models import Material

from .forms import AVATAR_PX, MAX_AVATAR_DATA
from .models import User, UserSession
from .sessions import REFRESH, SEEN_KEY


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
        self.assertNotContains(response, "Написать")

    def test_phone_is_shown_to_its_owner_only(self):
        self.person.phone = "89990001122"
        self.person.save(update_fields=["phone"])

        self.assertNotContains(self.client.get(self.url()), "89990001122")
        self.client.force_login(self.person)
        self.assertContains(self.client.get(self.url()), "89990001122")


class ContributionTests(TestCase):
    """Счётчики профиля: что считаем, а что нет."""

    @classmethod
    def setUpTestData(cls):
        cls.person = make_user("p@t.local")
        cls.viewer = make_user("v@t.local")
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.terms = {n: Term.objects.create(number=n) for n in (1, 2)}

    def setUp(self):
        self.client.force_login(self.viewer)

    def make(self, *terms, status=Material.Status.APPROVED, hidden=False):
        material = Material.objects.create(
            title="Конспект", subject=self.subject, uploader=self.person,
            status=status, hide_uploader=hidden,
        )
        material.terms.set([self.terms[n] for n in terms])
        return material

    def stats(self, viewer=None):
        self.client.force_login(viewer or self.viewer)
        return self.client.get(reverse("profile", args=[self.person.pk])).context["stats"]

    def test_only_published_work_counts(self):
        self.make(1)
        self.make(1, status=Material.Status.PENDING)
        self.make(1, status=Material.Status.REJECTED)

        self.assertEqual(self.stats()["materials"], 1)

    def test_anonymous_work_is_hidden_from_strangers_even_as_a_number(self):
        # Счётчик у человека с одной анонимной работой — это и есть снятая им подпись.
        self.make(1, hidden=True)
        self.make(1)

        self.assertEqual(self.stats()["materials"], 1)
        self.assertEqual(self.stats(self.person)["materials"], 2)

    def test_material_of_two_terms_lands_in_both(self):
        self.make(1, 2)
        self.make(2)

        self.assertEqual(
            [(row["label"], row["count"]) for row in self.stats()["by_term"]],
            [("1 семестр", 1), ("2 семестр", 2)],
        )

    def test_material_without_a_term_gets_its_own_row(self):
        self.make()

        self.assertEqual(self.stats()["by_term"], [{"label": "Без семестра", "count": 1, "share": 100}])


def make_gif(name="аватар.gif", frames=3):
    """Настоящая анимация: кадры должны РАЗЛИЧАТЬСЯ, одинаковые Pillow схлопнет в один."""
    pages = []
    for step in range(frames):
        page = PilImage.new("RGB", (40, 40), "black")
        page.paste(PilImage.new("RGB", (10, 40), "red"), (step * 10, 0))
        pages.append(page.convert("P"))

    buffer = BytesIO()
    pages[0].save(buffer, format="GIF", save_all=True, append_images=pages[1:], duration=100, loop=0)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/gif")


def data_url(size=(900, 300), mode="RGB", fmt="JPEG"):
    """Аватар в том виде, в каком его присылает браузер: готовый кусок картинки строкой."""
    buffer = BytesIO()
    PilImage.new(mode, size, "red").save(buffer, format=fmt)
    kind = "jpeg" if fmt == "JPEG" else "png"
    return f"data:image/{kind};base64," + b64encode(buffer.getvalue()).decode()


class ProfileEditTests(TestCase):
    def setUp(self):
        self.person = make_user("me@t.local")
        self.client.force_login(self.person)

    def post(self, **extra):
        response = self.client.post(reverse("profile_edit"), extra)
        self.person.refresh_from_db()
        return response

    def blob(self):
        """Сохранённый файл в памяти. Через FieldFile нельзя: у гифки Pillow держит
        его открытым ради перемотки кадров, и на windows каталог потом не удаляется."""
        with self.person.photo.open("rb") as handle:
            return BytesIO(handle.read())

    def saved(self):
        with PilImage.open(self.blob()) as image:
            return image.size, image.format

    def test_a_wide_photo_is_cut_down_to_a_square(self):
        self.post(photo=data_url((900, 300)))

        size, fmt = self.saved()
        self.assertEqual(size, (300, 300))
        self.assertEqual(fmt, "JPEG")

    def test_a_huge_photo_is_shrunk_to_the_thumbnail_side(self):
        self.post(photo=data_url((1500, 1500)))

        self.assertEqual(self.saved()[0], (AVATAR_PX, AVATAR_PX))

    def test_transparency_survives(self):
        # Аватары с дырками — обычное дело, а плоский белый квадрат в тёмной теме
        # выглядел бы заплаткой.
        self.post(photo=data_url((400, 400), mode="RGBA", fmt="PNG"))

        self.assertEqual(self.saved()[1], "PNG")
        with PilImage.open(self.blob()) as image:
            self.assertIn("A", image.mode)

    def test_empty_field_leaves_the_photo_alone(self):
        self.post(photo=data_url())
        was = self.person.photo.name

        self.post(photo="")

        self.assertEqual(self.person.photo.name, was)

    def test_photo_can_be_taken_off(self):
        self.post(photo=data_url())
        storage, was = self.person.photo.storage, self.person.photo.name

        self.post(photo="clear")

        self.assertFalse(self.person.photo)
        self.assertFalse(storage.exists(was))

    def test_replaced_photo_leaves_no_orphan_in_storage(self):
        self.post(photo=data_url())
        storage, was = self.person.photo.storage, self.person.photo.name

        self.post(photo=data_url((400, 400)))

        self.assertNotEqual(self.person.photo.name, was)
        self.assertFalse(storage.exists(was))

    def test_something_that_is_not_a_picture_is_refused(self):
        response = self.post(photo="data:image/png;base64," + b64encode(b"not a picture").decode())

        self.assertContains(response, "прочитать картинку")
        self.assertFalse(self.person.photo)

    def test_a_picture_too_heavy_for_the_post_is_refused(self):
        response = self.post(photo="data:image/png;base64," + "A" * MAX_AVATAR_DATA)

        self.assertContains(response, "тяжёлая")

    def test_a_gif_is_kept_whole_and_still_moves(self):
        # Канвас забрал бы из неё один кадр — анимация пропала бы молча.
        self.post(photo_file=make_gif())

        self.assertTrue(self.person.photo.name.endswith(".gif"))
        with PilImage.open(self.blob()) as image:
            self.assertEqual(image.n_frames, 3)

    def test_a_gif_replaces_a_cropped_photo_and_the_other_way_round(self):
        self.post(photo=data_url())
        self.post(photo_file=make_gif())
        self.assertTrue(self.person.photo.name.endswith(".gif"))

        self.post(photo=data_url())

        self.assertTrue(self.person.photo.name.endswith(".jpg"))

    def test_only_gifs_may_go_through_as_a_file(self):
        # Иначе этим полем заливалась бы любая картинка любого размера, минуя кадрирование.
        response = self.post(photo_file=make_image("big.png"))

        self.assertContains(response, "только гифки")
        self.assertFalse(self.person.photo)

    def test_links_are_reduced_to_handles(self):
        # Люди приносят ссылку целиком, а шаблон приклеивает её к адресу второй раз.
        self.post(tg_page="https://t.me/ivan", vk_page="@petya")

        self.assertEqual(self.person.tg_page, "ivan")
        self.assertEqual(self.person.vk_page, "petya")

    def test_name_cannot_be_changed_here(self):
        # Имя стоит подписью под каждым материалом и отзывом — меняет его администратор.
        self.post(name="Пётр", surname="Петров", email="new@t.local")

        self.assertEqual((self.person.name, self.person.surname), ("Иван", "Иванов"))
        self.assertEqual(self.person.email, "me@t.local")

    def test_stranger_cannot_be_edited_at_all(self):
        # Чужой профиль правит не «другой адрес», а никакой: форма всегда про себя.
        other = make_user("other@t.local")
        self.post(phone="89990001122")
        other.refresh_from_db()

        self.assertEqual(self.person.phone, "89990001122")
        self.assertEqual(other.phone, "")


CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64) AppleWebKit/537 Chrome/120 Safari/537"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605 Version/17 Safari/604"


class UserSessionTests(TestCase):
    """Учёт сессий: заводится входом, живёт вместе с самой сессией, закрывается хозяином."""

    def setUp(self):
        self.person = make_user("me@t.local")

    def enter(self, client=None, agent=CHROME, ip="10.0.0.7"):
        """Через настоящую форму входа: client.login() собирает запрос без заголовков,
        и ни адреса, ни браузера в нём нет — проверять было бы нечего."""
        client = client or self.client
        client.post(
            reverse("login"), {"username": "me@t.local", "password": "pass12345"},
            headers={"user-agent": agent}, REMOTE_ADDR=ip,
        )
        return client

    def rows(self):
        return UserSession.objects.filter(user=self.person)

    def test_login_leaves_a_trace_with_address_and_browser(self):
        self.enter()

        row = self.rows().get()
        self.assertEqual(row.session_id, self.client.session.session_key)
        self.assertEqual(row.ip, "10.0.0.7")
        self.assertEqual(row.where(), "Chrome · Windows")

    def test_edge_is_not_mistaken_for_chrome(self):
        # Edge представляется и хромом тоже — частные имена обязаны стоять раньше общих.
        self.enter(agent=CHROME + " Edg/120")

        self.assertEqual(self.rows().get().where(), "Edge · Windows")

    def test_the_trace_dies_with_the_session(self):
        # Сторожа за протухшими записями нет — их уносит каскад от самой сессии.
        self.enter()
        Session.objects.all().delete()

        self.assertFalse(self.rows().exists())

    def test_logging_out_removes_the_device_from_the_list(self):
        self.enter()

        self.client.post(reverse("logout"))

        self.assertFalse(self.rows().exists())

    def test_every_browser_gets_its_own_line(self):
        self.enter()
        self.enter(Client(), agent=IPHONE)

        self.assertEqual(self.rows().count(), 2)
        self.assertIn("Safari · iPhone", [row.where() for row in self.rows()])

    def test_profile_shows_only_my_own_devices(self):
        self.enter()
        stranger = make_user("other@t.local")
        Client().login(email="other@t.local", password="pass12345")

        response = self.client.get(reverse("profile", args=[self.person.pk]))
        alone = self.client.get(reverse("profile", args=[stranger.pk]))

        self.assertEqual(len(response.context["devices"]), 1)
        self.assertEqual(alone.context["devices"], [])

    def test_the_current_session_is_marked_and_has_no_close_button(self):
        self.enter()

        (row, current), = self.client.get(reverse("profile", args=[self.person.pk])).context["devices"]

        self.assertTrue(current)
        self.assertEqual(row.session_id, self.client.session.session_key)

    def test_the_key_never_reaches_the_page(self):
        # Ключ сессии — пароль на предъявителя: попав на скриншот, он работает до конца срока.
        self.enter()

        response = self.client.get(reverse("profile", args=[self.person.pk]))

        self.assertNotContains(response, self.client.session.session_key)

    def test_closing_one_device_leaves_the_others_alone(self):
        self.enter()
        other = self.enter(Client())
        doomed = self.rows().exclude(session_id=self.client.session.session_key).get()

        self.client.post(reverse("session_end"), {"id": doomed.pk})

        self.assertEqual(self.rows().count(), 1)
        # Тот браузер больше не вошедший — сайт закрытый, значит уводит на вход.
        self.assertEqual(other.get(reverse("profile", args=[self.person.pk])).status_code, 302)

    def test_closing_the_rest_keeps_me_logged_in(self):
        self.enter()
        self.enter(Client())
        self.enter(Client())

        self.client.post(reverse("session_end"))

        row = self.rows().get()
        self.assertEqual(row.session_id, self.client.session.session_key)

    def test_a_stranger_cannot_close_my_session(self):
        self.enter()
        mine = self.rows().get()
        thief = Client()
        make_user("thief@t.local")
        thief.login(email="thief@t.local", password="pass12345")

        thief.post(reverse("session_end"), {"id": mine.pk})

        self.assertTrue(self.rows().filter(pk=mine.pk).exists())

    def test_the_list_is_not_closed_by_a_get(self):
        # Иначе достаточно было бы заманить человека на картинку по этому адресу.
        self.enter()

        response = self.client.get(reverse("session_end"))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(self.rows().exists())

    def test_activity_is_refreshed_but_not_on_every_request(self):
        self.enter()
        url = reverse("profile", args=[self.person.pk])
        stale = timezone.now() - timedelta(hours=1)

        # Окно истекло — отметку положено освежить.
        self.age(REFRESH + 1)
        UserSession.objects.filter(user=self.person).update(seen=stale)
        self.client.get(url)
        self.assertGreater(self.rows().get().seen, stale)

        # Окно свежее — запроса в базу быть не должно, даже если отметка старая.
        UserSession.objects.filter(user=self.person).update(seen=stale)
        self.client.get(url)
        self.assertEqual(self.rows().get().seen, stale)

    def age(self, seconds):
        """Состарить отметку в самой сессии — по ней и решается, пора ли писать в базу."""
        session = self.client.session
        session[SEEN_KEY] = time.time() - seconds
        session.save()


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
