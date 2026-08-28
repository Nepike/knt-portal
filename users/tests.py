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
from core.models import ALUMNI, Subject, Team, Term
from economy.models import BalanceLog
from economy.services import credit, spend
from materials.models import Material

from .forms import AVATAR_PX, MAX_AVATAR_DATA
from .models import STATUS_MAX, User, UserSession
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

    def test_the_status_is_saved_and_shown_to_everyone(self):
        self.post(status="Сплю на парах")

        self.assertEqual(self.person.status, "Сплю на парах")
        stranger = Client()
        stranger.force_login(make_user("other@t.local"))
        self.assertContains(stranger.get(reverse("profile", args=[self.person.pk])), "Сплю на парах")

    def test_the_status_stays_one_line(self):
        # Иначе подпись растянется на полстраницы и разъедет шапку профиля.
        self.post(status="  первая \n\n  вторая  ")

        self.assertEqual(self.person.status, "первая вторая")

    def test_a_status_longer_than_the_line_is_refused(self):
        self.post(status="я" * (STATUS_MAX + 1))

        self.assertEqual(self.person.status, "")

    def test_an_empty_status_shows_nothing_at_all(self):
        """«Пользователь не указал статус» — это шум, а не сведения."""
        page = self.client.get(reverse("profile", args=[self.person.pk])).content.decode()

        self.assertNotIn("татус", page)

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

    def test_a_broken_id_closes_nothing_and_does_not_fall_over(self):
        # filter(pk="ой") — это ValueError и пятисотка; закрывать всё подряд тоже нельзя.
        self.enter()
        self.enter(Client())

        response = self.client.post(reverse("session_end"), {"id": "ой"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows().count(), 2)

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


class StudentListTests(TestCase):
    """Раздел заведён, чтобы НАЙТИ человека. Топ — приятная добавка, но главное поиск."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(
            number="Б05-123", profile="Программирование", course_code="03.03.01",
            stage="bachelor", year_of_admission=2023,
        )
        cls.ivan = cls.person("i@t.local", "Иван", "Иванов", team=cls.team)
        cls.petr = cls.person("p@t.local", "Пётр", "Петров", team=cls.team)
        cls.anna = cls.person("a@t.local", "Анна", "Сидорова")

    @staticmethod
    def person(email, name, surname, **extra):
        return User.objects.create_user(
            email=email, name=name, surname=surname, password="pass12345",
            must_change_password=False, **extra,
        )

    def setUp(self):
        self.client.force_login(self.ivan)

    def get(self, **params):
        return self.client.get(reverse("student_list"), params)

    def earn(self, person, amount):
        credit(person, amount, BalanceLog.Reason.MATERIAL, key=str(amount))

    def names(self, response):
        return [person.full_name for person in response.context["people"]]

    def test_the_page_lists_everyone(self):
        self.assertEqual(self.names(self.get()), ["Иванов Иван", "Петров Пётр", "Сидорова Анна"])

    def test_a_person_is_found_by_name_or_surname(self):
        self.assertEqual(self.names(self.get(q="Петров")), ["Петров Пётр"])
        self.assertEqual(self.names(self.get(q="Анна")), ["Сидорова Анна"])

    def test_the_search_puts_the_closest_hit_first(self):
        """Порядок поиска идёт ПЕРВЫМ ключом, выбранный — после него. По «ван» сначала
        Ванин, у которого слово в начале фамилии, и только потом Иванов, где оно
        в середине, — сколько бы Иванов ни заработал (`core.search`)."""
        vanin = self.person("v@t.local", "Сергей", "Ванин")
        self.earn(self.ivan, 5000)

        found = self.get(q="ван", sort="contribution")

        self.assertEqual(self.names(found), [vanin.full_name, self.ivan.full_name])

    def test_nobody_found_says_so(self):
        self.assertContains(self.get(q="Сорокоумов"), "никого не нашлось")

    def test_by_default_the_list_is_alphabetical(self):
        self.earn(self.anna, 900)

        self.assertEqual(self.names(self.get())[0], "Иванов Иван")

    def test_by_contribution_the_biggest_goes_first(self):
        # Суммы заведомо больше стартовых: их получает каждый, кто хоть раз вошёл.
        self.earn(self.anna, 9000)
        self.earn(self.petr, 5000)

        self.assertEqual(self.names(self.get(sort="contribution")), [
            "Сидорова Анна", "Петров Пётр", "Иванов Иван",
        ])

    def test_it_counts_what_was_earned_and_not_what_is_left(self):
        """Баланс — это заработанное минус потраченное, и топ по нему был бы топом тех,
        кто ничего не покупает: купил рамку — уехал вниз."""
        self.earn(self.anna, 900)
        spend(self.anna, 850, BalanceLog.Reason.PURCHASE)
        self.earn(self.petr, 100)

        listed = self.get(sort="contribution").context["people"]

        self.assertEqual([person.full_name for person in listed][0], "Сидорова Анна")
        self.assertEqual(listed[0].earned, 900)
        self.assertEqual(listed[0].wallet.balance, 50)

    def test_a_junk_sort_does_not_break_the_page(self):
        self.assertEqual(self.get(sort="; drop table").status_code, 200)

    def test_the_top_shows_who_did_the_most(self):
        self.earn(self.anna, 9000)
        self.earn(self.petr, 5000)

        top = self.get().context["top"]

        self.assertEqual([person.full_name for person in top], [
            "Сидорова Анна", "Петров Пётр", "Иванов Иван",
        ])

    def test_a_person_who_left_is_not_listed(self):
        User.objects.filter(pk=self.petr.pk).update(is_active=False)

        self.assertNotIn("Петров Пётр", self.names(self.get()))

    def test_a_row_leads_to_the_profile_and_names_the_group(self):
        page = self.get().content.decode()

        self.assertIn(reverse("profile", args=[self.ivan.pk]), page)
        self.assertIn(self.team.number, page)
        self.assertIn(self.team.get_grade_str(), page)
        self.assertIn("Без группы", page)  # у Анны группы нет

    def test_an_alumnus_is_not_given_a_made_up_group_number(self):
        """У служебной группы выпускников номер «000000», и «Выпускник · 000000»
        читается как поломка."""
        alumni = Team.objects.create(
            number="000000", profile="Выпускники", course_code="—",
            stage="bachelor", year_of_admission=Team.ALUMNI_YEAR,
        )
        User.objects.filter(pk=self.anna.pk).update(team=alumni)

        rows = self.get().content.decode().split('id="student-list"')[1]

        self.assertIn("Выпускник", rows)
        self.assertNotIn(alumni.number, rows)

    def test_the_service_group_is_not_offered_as_a_group(self):
        """Её номер «000000» ничего не значит, а отбор по ней — это ровно курс
        «Выпускники», который тут же рядом."""
        alumni = Team.objects.create(
            number="000000", profile="Выпускники", course_code="—",
            stage="bachelor", year_of_admission=Team.ALUMNI_YEAR,
        )

        listed = self.get().context["form"].fields["team"].queryset

        self.assertIn(self.team, listed)
        self.assertNotIn(alumni, listed)

    def test_a_live_search_answers_with_the_list_alone(self):
        chunk = self.client.get(reverse("student_list"), {"q": "Петров"}, headers={"HX-Request": "true"})

        self.assertContains(chunk, "Петров Пётр")
        self.assertNotContains(chunk, "Поиск студентов")  # шапка страницы не переезжает

    def test_the_course_filter_keeps_only_that_year(self):
        """Курс нигде не хранится — он считается от года зачисления группы."""
        older = Team.objects.create(
            number="Б05-999", profile="Программирование", course_code="03.03.01",
            stage="bachelor", year_of_admission=self.team.year_of_admission - 2,
        )
        User.objects.filter(pk=self.petr.pk).update(team=older)

        found = self.get(course=str(self.team.get_grade_level()))

        self.assertEqual(self.names(found), ["Иванов Иван"])

    def test_the_courses_offered_are_the_ones_that_exist(self):
        """Список не зашит: набор групп меняется каждый год, и «6 курс», за которым
        никого нет, — предложение, ведущее в пустоту."""
        offered = [value for value, _ in self.get().context["form"].fields["course"].choices if value]

        self.assertEqual(offered, [str(self.team.get_grade_level())])

    def test_graduates_are_a_bucket_of_their_own(self):
        alumni = Team.objects.create(
            number="000000", profile="Выпускники", course_code="—",
            stage="bachelor", year_of_admission=Team.ALUMNI_YEAR,
        )
        User.objects.filter(pk=self.anna.pk).update(team=alumni)

        self.assertEqual(self.names(self.get(course=ALUMNI)), ["Сидорова Анна"])

    def test_the_group_filter_keeps_only_its_members(self):
        self.assertEqual(self.names(self.get(team=self.team.pk)), ["Иванов Иван", "Петров Пётр"])

    def test_a_group_from_another_course_narrows_to_nothing(self):
        """Фильтры складываются: курс И группа. Тупик тут честнее, чем молчаливое
        игнорирование одного из двух."""
        older = Team.objects.create(
            number="Б05-999", profile="Программирование", course_code="03.03.01",
            stage="bachelor", year_of_admission=self.team.year_of_admission - 2,
        )

        found = self.get(course=str(self.team.get_grade_level()), team=older.pk)

        self.assertEqual(self.names(found), [])
        # И говорим об этом честно: «пока никого» тут читалось бы как «людей нет вовсе».
        self.assertContains(found, "Под такой подбор никто не попал")

    def test_the_group_list_shrinks_to_the_chosen_course(self):
        older = Team.objects.create(
            number="Б05-999", profile="Программирование", course_code="03.03.01",
            stage="bachelor", year_of_admission=self.team.year_of_admission - 2,
        )

        listed = self.get(course=str(self.team.get_grade_level())).context["form"].fields["team"].queryset

        self.assertIn(self.team, listed)
        self.assertNotIn(older, listed)

    def test_the_chosen_group_stays_in_the_list_even_off_course(self):
        """Иначе своего же значения в списке не оказалось бы и сменить его было бы нечем."""
        older = Team.objects.create(
            number="Б05-999", profile="Программирование", course_code="03.03.01",
            stage="bachelor", year_of_admission=self.team.year_of_admission - 2,
        )

        found = self.get(course=str(self.team.get_grade_level()), team=older.pk)

        self.assertIn(older, found.context["form"].fields["team"].queryset)

    def test_junk_in_the_filters_does_not_break_the_page(self):
        self.assertEqual(self.get(course="; drop table", team="ой").status_code, 200)
        self.assertEqual(self.names(self.get(course="; drop table")), self.names(self.get()))

    def test_changing_the_course_brings_the_filters_back_with_the_list(self):
        """Набор групп в селекте после смены курса другой — без oob-замены он остался бы
        прежним, и в нём предлагались бы чужие группы."""
        chunk = self.client.get(
            reverse("student_list"), {"course": str(self.team.get_grade_level())},
            headers={"HX-Request": "true"},
        )

        self.assertContains(chunk, 'id="student-filters"')
        self.assertContains(chunk, "hx-swap-oob")
        self.assertIn("course=", chunk["HX-Push-Url"])

    def test_loading_the_next_batch_does_not_touch_the_filters(self):
        """Каждая порция иначе перерисовывала бы селекты — и сбрасывала бы открытый список."""
        chunk = self.client.get(reverse("student_list"), {"page": 1}, headers={"HX-Request": "true"})

        self.assertNotContains(chunk, "hx-swap-oob")

    def test_the_menu_leads_here_and_lights_up(self):
        page = self.get()

        self.assertEqual(page.context["section"], "students")
        self.assertIn(f'href="{reverse("student_list")}"', page.content.decode())

    def test_the_profile_does_not_light_the_section(self):
        """В профиль приходят отовсюду — из чата, из ленты отзывов; подсвеченные
        «Студенты» соврали бы про то, откуда человек пришёл."""
        page = self.client.get(reverse("profile", args=[self.ivan.pk]))

        self.assertEqual(page.context["section"], "")
