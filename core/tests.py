import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.core.management import call_command
from django.core.mail import EmailMessage, EmailMultiAlternatives, send_mail
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from PIL import Image as PilImage

from knt.celery import app as celery_app
from core.models import Team
from teachers.models import Review, Teacher
from users.models import User

from chats.models import Chat

from . import beta
from .legacy_markup import to_markdown
from .management.commands.import_legacy_files import extension, filename
from .search import by_name
from .throttle import throttled
from .markup import render
from .tasks import ping


class CeleryTests(SimpleTestCase):
    """Очередь: задачи находятся автоматически и доезжают до исполнения.
    В тестах — на месте (task_always_eager из core/test_runner.py), без Redis и воркера."""

    def test_task_is_found_by_autodiscovery(self):
        # Ломается, если из knt/__init__.py уйдёт импорт celery_app: тогда задач просто нет.
        self.assertIn("core.tasks.ping", celery_app.tasks)

    def test_task_runs_and_returns_its_answer(self):
        self.assertEqual(ping.delay("эхо").get(), "эхо")


# Django на время тестов сам подменяет EMAIL_BACKEND на locmem, поэтому очередь
# для писем включаем явно: иначе эта ветка кода в тестах вообще не работала бы.
@override_settings(
    EMAIL_BACKEND="core.mail.QueuedEmailBackend",
    EMAIL_DELIVERY_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class QueuedMailTests(TestCase):
    def test_letter_reaches_the_mailbox_through_the_queue(self):
        send_mail("Тема", "Текст", None, ["s@t.local"])

        self.assertEqual(len(mail.outbox), 1)
        letter = mail.outbox[0]
        self.assertEqual(letter.subject, "Тема")
        self.assertEqual(letter.body, "Текст")
        self.assertEqual(letter.to, ["s@t.local"])

    def test_html_part_and_headers_survive_the_trip(self):
        letter = EmailMultiAlternatives("Тема", "Текст", to=["s@t.local"], headers={"X-Kind": "test"})
        letter.attach_alternative("<b>Текст</b>", "text/html")
        letter.send()

        sent = mail.outbox[0]
        self.assertEqual(sent.alternatives[0].content, "<b>Текст</b>")
        self.assertEqual(sent.extra_headers["X-Kind"], "test")

    def test_attachment_is_refused_loudly(self):
        # Молча потерять вложение хуже, чем упасть: тела задач лежат в Redis,
        # и складывать туда файлы — отдельное решение, а не побочный эффект.
        letter = EmailMessage("Тема", "Текст", to=["s@t.local"])
        letter.attach("файл.txt", "данные".encode(), "text/plain")
        with self.assertRaises(ValueError):
            letter.send()

    def test_password_reset_letter_goes_through_the_queue(self):
        User.objects.create_user(
            email="student@t.local", name="Иван", surname="Иванов",
            password="pass12345", must_change_password=False,
        )
        self.client.post(reverse("password_reset"), {"email": "student@t.local"})

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["student@t.local"])


class LegacyMarkupTests(SimpleTestCase):
    """Тексты старого сайта (Quill Delta и старый HTML) → markdown."""

    def delta(self, *ops):
        return to_markdown(json.dumps({"ops": list(ops)}))

    def test_empty_delta_gives_empty_text(self):
        # Так выглядит 1134 материала из 1523: редактор оставил один перенос строки.
        self.assertEqual(self.delta({"insert": "\n"}), "")
        self.assertEqual(to_markdown("<p><br></p>"), "")
        self.assertEqual(to_markdown(""), "")

    def test_block_attributes_come_from_the_newline(self):
        self.assertEqual(
            self.delta({"insert": "Заголовок"}, {"attributes": {"header": 3}, "insert": "\n"}),
            "### Заголовок",
        )
        self.assertEqual(
            self.delta({"insert": "пункт"}, {"attributes": {"list": "ordered"}, "insert": "\n"}),
            "1. пункт",
        )

    def test_inline_styles_do_not_swallow_spaces(self):
        # «** текст **» markdown жирным не считает.
        self.assertEqual(self.delta({"attributes": {"bold": True}, "insert": "жирно "}), "**жирно**")

    def test_link_keeps_its_address(self):
        markdown = self.delta({"attributes": {"link": "http://e.com"}, "insert": "тут"})
        self.assertEqual(markdown, "[тут](http://e.com)")

    def test_formula_becomes_dollars(self):
        self.assertEqual(self.delta({"insert": {"formula": "x^2"}}), "$x^2$")

    def test_consecutive_code_lines_make_one_fence(self):
        markdown = self.delta(
            {"insert": "a"}, {"attributes": {"code-block": True}, "insert": "\n"},
            {"insert": "b"}, {"attributes": {"code-block": True}, "insert": "\n"},
        )
        self.assertEqual(markdown, "```\na\nb\n```")

    def test_dash_is_escaped_only_at_the_start_of_a_line(self):
        # В середине фразы это тире, и «\-» показалось бы читателю как есть.
        self.assertEqual(self.delta({"insert": "тут - тире"}), "тут - тире")
        self.assertEqual(self.delta({"insert": "- не список"}), r"\- не список")

    def test_html_formula_is_taken_from_the_source_not_the_rendering(self):
        raw = (
            '<p><span class="ql-formula" data-value="a+b">\ufeff'
            '<span class="katex"><span class="katex-mathml">МУСОР</span></span></span> итого</p>'
        )
        self.assertEqual(to_markdown(raw), "$a+b$ итого")

    def test_html_paragraphs_and_lists(self):
        raw = "<p>Первый</p><ol><li>раз</li><li>два</li></ol>"
        self.assertEqual(to_markdown(raw), "Первый\n\n1. раз\n1. два")

    def test_result_survives_the_site_renderer(self):
        markdown = self.delta({"insert": {"formula": "x_1"}}, {"insert": "\n"})
        self.assertIn("arithmatex", render(markdown))


class AlumniTeamTests(TestCase):
    """Служебная группа выпускников: год 0 — метка «потока нет»."""

    def team(self, **extra):
        return Team.objects.create(
            number=extra.pop("number", "000000"), profile="Выпускники", course_code="000000",
            stage="bachelor", year_of_admission=Team.ALUMNI_YEAR, **extra,
        )

    def test_signature_has_no_bogus_year(self):
        # Обычный расчёт дал бы «Выпускник 6 года»: 0 + 6 лет бакалавриата.
        self.assertEqual(self.team().get_grade_str(), "Выпускник")

    def test_ordinary_team_still_reports_its_year(self):
        ordinary = Team.objects.create(
            number="Б07-001", profile="ФБМФ", course_code="03.03.01",
            stage="bachelor", year_of_admission=2015,
        )
        self.assertEqual(ordinary.get_grade_str(), "Выпускник 2021 года")

    def test_course_chat_is_named_for_people_not_for_a_year(self):
        self.assertEqual(Chat.course_title("bachelor", Team.ALUMNI_YEAR), "Выпускники")
        self.assertEqual(Chat.course_title("bachelor", 2024), "Бакалавриат 2024")

    def test_cleanup_never_takes_the_alumni_team(self):
        # По обычному правилу её «выпуск» пришёлся бы на шестой год нашей эры.
        alumni = self.team()
        call_command("cleanup_legacy", "--apply", verbosity=0)
        self.assertTrue(Team.objects.filter(pk=alumni.pk).exists())


def make_user(email, **extra):
    extra.setdefault("name", "Иван")
    extra.setdefault("surname", "Иванов")
    return User.objects.create_user(email=email, password="pass12345", must_change_password=False, **extra)


def make_image(name="скриншот.png"):
    buffer = BytesIO()
    PilImage.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class PeopleSearchTests(TestCase):
    """Поиск по имени — один на весь сайт, core/search.py."""

    @classmethod
    def setUpTestData(cls):
        cls.maxim = make_user("m@x.ru", name="Максим", surname="Щучкин")
        cls.kate = make_user("k@x.ru", name="Екатерина", surname="Бажанова", patronymic="Максимовна")
        cls.koval = make_user("kv@x.ru", name="Пётр", surname="Ковалёв")
        cls.volkov = make_user("v@x.ru", name="Сергей", surname="Волков")

    def found(self, query, **kwargs):
        return [user.email for user in by_name(User.objects.all(), query, **kwargs)]

    def test_a_full_name_is_found_whatever_the_order_of_the_words(self):
        for query in ("Максим Щучкин", "Щучкин Максим", "макс щуч"):
            with self.subTest(query=query):
                self.assertEqual(self.found(query), [self.maxim.email])

    def test_the_patronymic_does_not_drag_in_a_stranger(self):
        self.assertNotIn(self.kate.email, self.found("Максим"))

    def test_but_it_does_when_asked_for_it(self):
        fields = ("surname", "name", "patronymic")
        self.assertIn(self.kate.email, self.found("Бажанова Екатерина Максимовна", fields=fields))

    def test_those_whose_name_begins_with_the_word_go_first(self):
        # Список обрезан десятком, поэтому порядок решает, попадёт ли нужный человек в него.
        self.assertEqual(self.found("ков"), [self.koval.email, self.volkov.email])

    def test_yo_and_ye_are_the_same_letter_on_both_sides(self):
        # В базе есть и «Пётр», и «Петр» — а в поиске пишут как придётся.
        self.assertEqual(self.found("ковалев"), [self.koval.email])
        self.assertEqual(self.found("КОВАЛЁВ"), [self.koval.email])

    def test_an_empty_query_changes_nothing(self):
        self.assertEqual(len(self.found("   ")), User.objects.count())


class HtmxVaryTests(TestCase):
    """По одному адресу у нас два ответа — страница и кусок разметки для htmx."""

    def setUp(self):
        self.client.force_login(make_user("reader@x.ru"))

    def test_both_answers_tell_the_browser_they_are_different(self):
        # Без этого браузер по «назад» рисовал кусок целым документом: без шапки,
        # меню и фильтров — их-то и «сбрасывало».
        page = self.client.get(reverse("material_list"))
        chunk = self.client.get(reverse("material_list"), headers={"HX-Request": "true"})
        self.assertNotEqual(page.content, chunk.content)
        for response in (page, chunk):
            self.assertIn("HX-Request", response.headers["Vary"])


@override_settings(BETA=True)
class BetaLockTests(TestCase):
    """Закрыта геймификация: профиль со всем, что к нему прицеплено, и магазин.

    Решение пользователя: открывать не по частям, а разом — когда будут ещё кейсы и значки.
    """

    def setUp(self):
        self.reader = make_user("reader@x.ru")
        self.staff = make_user("staff@x.ru", is_staff=True)
        self.client.force_login(self.reader)

    def test_every_other_section_is_reachable(self):
        # Загрузка и правка тоже: «только чтение» кончилось вместе с проверкой разделов.
        for name in ("material_list", "book_list", "teacher_list", "support",
                     "chat_list", "material_new", "book_new"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_the_profile_explains_itself_instead_of_pretending_to_be_missing(self):
        # 403 со страницей, а не 404: страница существует, просто ещё не показывается.
        response = self.client.get(reverse("profile", args=[self.reader.pk]))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Страница ещё не готова", status_code=403)

    def test_everything_hanging_off_the_profile_is_closed_too(self):
        # Правка, сессии и экипировка живут на той же странице — открывать их поодиночке
        # значило бы оставить работающие ручки у закрытой двери.
        self.assertEqual(self.client.get(reverse("profile_edit")).status_code, 403)
        self.assertEqual(self.client.post(reverse("session_end")).status_code, 403)
        self.assertEqual(self.client.post(reverse("item_unequip")).status_code, 403)

    def test_the_shop_is_closed_together_with_the_profile(self):
        # Покупать, не видя купленного в профиле, нечего — открываться им только вместе.
        self.assertEqual(self.client.get(reverse("shop")).status_code, 403)
        self.assertEqual(self.client.post(reverse("item_buy", args=[1])).status_code, 403)

    def test_staff_walks_everywhere(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("profile", args=[self.staff.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("shop")).status_code, 200)

    def test_an_anonymous_visitor_is_sent_to_login_not_to_the_beta_page(self):
        self.client.logout()
        target = reverse("profile", args=[self.reader.pk])

        self.assertRedirects(self.client.get(target), f"{reverse('login')}?next={target}")

    def test_the_root_leads_to_materials(self):
        self.assertRedirects(self.client.get("/"), reverse("material_list"))

    @override_settings(BETA=False)
    def test_switching_beta_off_opens_everything(self):
        self.assertEqual(self.client.get(reverse("profile", args=[self.reader.pk])).status_code, 200)

    def test_the_menu_does_not_offer_a_page_that_is_closed(self):
        """Пункты гасим, а не прячем — пусть видно, что раздел есть и никуда не делся."""
        page = self.client.get(reverse("material_list")).content.decode()

        self.assertIn("Профиль", page)
        self.assertIn("Магазин", page)
        self.assertNotIn(reverse("profile", args=[self.reader.pk]), page)
        self.assertNotIn(f'href="{reverse("shop")}"', page)

    def test_staff_still_gets_the_links(self):
        # Словарь locked собран из того же списка, что и замок, значит и исключение для
        # персонала должно совпадать: иначе пункт серый, а страница открывается.
        self.client.force_login(self.staff)
        page = self.client.get(reverse("material_list")).content.decode()

        self.assertIn(reverse("profile", args=[self.staff.pk]), page)
        self.assertIn(f'href="{reverse("shop")}"', page)


@override_settings(BETA=True)
class BetaTeachersTests(TestCase):
    """Раздел преподавателей открыт ЦЕЛИКОМ — не только чтение, но и отзывы."""

    def setUp(self):
        self.reader = make_user("reader@x.ru")
        self.client.force_login(self.reader)
        self.teacher = Teacher.objects.create(name="Пётр", surname="Сорокоумов")

    def test_a_student_reads_the_card(self):
        self.assertEqual(self.client.get(reverse("teacher_detail", args=[self.teacher.pk])).status_code, 200)

    def test_a_student_leaves_a_review_and_can_take_it_back(self):
        url = reverse("teacher_detail", args=[self.teacher.pk])
        self.client.post(url, {"score_knowledge": 5, "text": "Объясняет понятно"})
        review = Review.objects.get(teacher=self.teacher, author=self.reader)
        self.assertEqual(review.text, "Объясняет понятно")

        self.assertEqual(self.client.post(reverse("review_like", args=[review.pk])).status_code, 200)
        self.assertEqual(review.liked_users.count(), 1)

        self.client.post(reverse("review_delete", args=[review.pk]))
        self.assertFalse(Review.objects.filter(pk=review.pk).exists())


@override_settings(BETA=True)
class SupportFormTests(TestCase):
    def setUp(self):
        self.user = make_user("reader@x.ru", name="Иван", surname="Петров")
        self.client.force_login(self.user)

    def test_a_report_reaches_the_support_chat(self):
        with patch("core.views.notify") as sent:
            response = self.client.post(reverse("support"), {
                "topic": "broken", "text": "Не открывается книга",
            })
        self.assertRedirects(response, reverse("support"))
        chat, template, context = sent.call_args.args
        self.assertEqual(chat, "support")
        self.assertEqual(template, "telegram/support.html")
        self.assertEqual(context["author"], self.user)
        self.assertEqual(context["topic"], "Что-то не работает")

    def test_an_empty_report_is_not_sent(self):
        with patch("core.views.notify") as sent:
            response = self.client.post(reverse("support"), {"topic": "broken", "text": ""})
        self.assertEqual(response.status_code, 200)
        sent.assert_not_called()

    def test_the_page_says_nothing_about_the_beta(self):
        # Страница переживёт бету — плашка на ней была бы мусором уже через месяц.
        page = self.client.get(reverse("support")).content.decode()
        self.assertNotIn("бета-версии", page)

    def test_a_logged_in_person_is_not_asked_for_a_contact(self):
        # Связаться есть как: в чат уезжает ссылка на профиль, а там телеграм и ВК.
        self.assertNotIn("contact", self.client.get(reverse("support")).context["form"].fields)

    def test_the_message_carries_who_wrote_and_how_to_answer(self):
        self.user.tg_page = "ivan"
        text = render_to_string("telegram/support.html", {
            "author": self.user, "profile_url": "https://knt-mipt.ru/users/1/",
            "topic": "Предложение", "text": "Добавьте тёмную тему",
        })
        self.assertIn("Добавьте тёмную тему", text)
        self.assertIn("https://knt-mipt.ru/users/1/", text)
        self.assertIn("Петров Иван", text)
        self.assertIn("https://t.me/ivan", text)
        # Почты в чате быть не должно, а страницы, с которой пришли, — тем более:
        # она попадала туда из referer и сбивала с толку.
        self.assertNotIn(self.user.email, text)
        self.assertNotIn("Страница:", text)

    def test_vk_stands_in_when_there_is_no_telegram(self):
        self.user.vk_page = "ivan_vk"
        text = render_to_string("telegram/support.html", {"author": self.user, "topic": "Другое", "text": "?"})
        self.assertIn("https://vk.com/ivan_vk", text)

    def test_without_any_contacts_the_message_is_just_shorter(self):
        text = render_to_string("telegram/support.html", {"author": self.user, "topic": "Другое", "text": "?"})
        self.assertIn("Петров Иван", text)
        self.assertNotIn("t.me", text)
        self.assertNotIn("vk.com", text)

    def test_a_picture_rides_along_with_the_report(self):
        with patch("core.views.notify") as sent:
            self.client.post(reverse("support"), {
                "topic": "broken", "text": "вот так это выглядит", "image": make_image(),
            })
        self.assertTrue(sent.call_args.kwargs["image"])


@override_settings(BETA=True)
class SupportWithoutLoginTests(TestCase):
    """Кто не может войти — как раз тот, кому поддержка нужнее всего."""

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты

    def test_the_page_opens_to_a_visitor_who_is_not_logged_in(self):
        self.assertEqual(self.client.get(reverse("support")).status_code, 200)

    def test_without_a_contact_there_would_be_nowhere_to_answer(self):
        with patch("core.views.notify") as sent:
            response = self.client.post(reverse("support"), {"topic": "account", "text": "Не приходит письмо"})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "contact", "Обязательное поле.")
        sent.assert_not_called()

    def test_a_visitor_with_a_contact_gets_through(self):
        with patch("core.views.notify") as sent:
            response = self.client.post(reverse("support"), {
                "topic": "account", "text": "Не приходит письмо", "contact": "ivan@mipt.ru",
            })
        self.assertRedirects(response, reverse("support"))
        context = sent.call_args.args[2]
        self.assertIsNone(context["author"])
        self.assertEqual(context["contact"], "ivan@mipt.ru")

    def test_the_message_shows_that_nobody_stands_behind_the_report(self):
        text = render_to_string("telegram/support.html", {
            "author": None, "topic": "Аккаунт и доступ", "text": "Не приходит письмо",
            "contact": "ivan@mipt.ru",
        })
        self.assertIn("гость", text)
        self.assertIn("ivan@mipt.ru", text)

    def test_a_flood_stops_reaching_the_chat(self):
        # Форма открыта всему интернету: без ограничителя чат завалило бы за вечер.
        payload = {"topic": "other", "text": "спам", "contact": "spam@x.ru"}
        with patch("core.views.notify") as sent:
            for _ in range(14):
                self.client.post(reverse("support"), payload)
        self.assertEqual(sent.call_count, 10)


class ThrottleTests(SimpleTestCase):
    """Ограничитель частоты: сколько пропускает и как ведёт себя без кэша."""

    def setUp(self):
        cache.clear()

    def test_the_limit_is_how_many_calls_get_through(self):
        self.assertEqual([not throttled("t:calls", 2) for _ in range(4)], [True, True, False, False])

    def test_keys_count_separately(self):
        for _ in range(3):
            throttled("t:one", 2)
        self.assertFalse(throttled("t:two", 2))

    def test_a_dead_cache_lets_people_through_instead_of_locking_them_out(self):
        """Redis лёг — забывший пароль не должен из-за этого остаться без входа.
        Ограничитель нужен против потока, а не вместо самой страницы."""
        with patch("core.throttle.cache.add", side_effect=ConnectionError("Redis не отвечает")):
            with self.assertLogs("core.throttle", "ERROR"):
                self.assertFalse(throttled("t:dead", 1))


class LegacyFileKeyTests(SimpleTestCase):
    """Имя внутри ключа хранилища: название записи плюс настоящее расширение."""

    def name(self, title, path):
        return filename(title, extension(Path(path)))

    def test_extension_comes_from_the_file_and_is_not_doubled(self):
        self.assertEqual(self.name("Матан", "12_0_Матан.pdf"), "Матан.pdf")
        self.assertEqual(self.name("Матан.pdf", "12_0_Матан.pdf.pdf"), "Матан.pdf")
        # Название врёт про формат — верим файлу: по расширению выбирается значок.
        self.assertEqual(self.name("Матан.pdf", "12_0_Матан.zip"), "Матан.pdf.zip")

    def test_a_dot_inside_the_title_is_not_an_extension(self):
        # «Порай-Кошиц М.А. Основы…» — иначе расширением станет полстроки.
        self.assertEqual(extension(Path("1_0_Порай-Кошиц М.А. Основы анализа")), "")
        self.assertEqual(self.name("Порай-Кошиц М.А. Основы", "1_0_Порай-Кошиц М.А. Основы"), "Порай-Кошиц М.А. Основы")

    def test_signs_windows_forbids_are_dropped(self):
        # Старый сайт жил на линуксе, и на этих названиях перенос падал с Errno 22.
        self.assertEqual(self.name('Кузьменко "Начала химии"', "1_0_x.pdf"), "Кузьменко Начала химии.pdf")
        self.assertEqual(self.name("Том 1: Функции", "1_0_x.pdf"), "Том 1 Функции.pdf")
        self.assertEqual(self.name("Билет 1 | 21 дек.", "1_0_x.pdf"), "Билет 1 21 дек.pdf")
        self.assertEqual(self.name("темы зачета/экзамена", "1_0_x.doc"), "темы зачета экзамена.doc")

    def test_a_title_that_is_all_punctuation_still_gives_a_name(self):
        self.assertEqual(self.name("...", "1_0_x.pdf"), "file.pdf")
        self.assertEqual(self.name("", "1_0_x"), "file")
