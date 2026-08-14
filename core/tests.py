import json
from pathlib import Path
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.template.loader import render_to_string
from django.core.management import call_command
from django.core.mail import EmailMessage, EmailMultiAlternatives, send_mail
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from knt.celery import app as celery_app
from core.models import Team
from users.models import User

from chats.models import Chat

from .legacy_markup import to_markdown
from .management.commands.import_legacy_files import extension, filename
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
        self.assertEqual(self.delta({"insert": "- не список"}), "\- не список")

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


@override_settings(BETA=True)
class BetaLockTests(TestCase):
    """Пока идёт бета, наружу открыты только материалы, библиотека и поддержка."""

    def setUp(self):
        self.reader = make_user("reader@x.ru")
        self.staff = make_user("staff@x.ru", is_staff=True)

    def test_open_sections_are_reachable(self):
        self.client.force_login(self.reader)
        for name in ("material_list", "book_list", "support"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_closed_sections_explain_themselves_instead_of_pretending_to_be_missing(self):
        # 403 со страницей, а не 404: раздел существует, просто ещё закрыт.
        self.client.force_login(self.reader)
        for name in ("teacher_list", "chat_list", "wall", "material_new", "book_new"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 403, name)
            self.assertContains(response, "Раздел ещё закрыт", status_code=403)

    def test_staff_walks_everywhere(self):
        self.client.force_login(self.staff)
        for name in ("teacher_list", "chat_list", "material_new", "book_new"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_an_anonymous_visitor_is_sent_to_login_not_to_the_beta_page(self):
        response = self.client.get(reverse("teacher_list"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('teacher_list')}")

    def test_the_root_leads_to_materials(self):
        self.client.force_login(self.reader)
        self.assertRedirects(self.client.get("/"), reverse("material_list"))

    @override_settings(BETA=False)
    def test_switching_beta_off_opens_everything(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("teacher_list")).status_code, 200)


@override_settings(BETA=True)
class SupportFormTests(TestCase):
    def setUp(self):
        self.user = make_user("reader@x.ru", name="Иван", surname="Петров")
        self.client.force_login(self.user)

    def test_a_report_reaches_the_support_chat(self):
        with patch("core.views.notify") as sent:
            response = self.client.post(reverse("support"), {
                "topic": "broken", "text": "Не открывается книга", "page": "/library/12/",
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

    def test_the_contact_is_prefilled_for_those_we_already_know(self):
        form = self.client.get(reverse("support")).context["form"]
        self.assertEqual(form.fields["contact"].initial, self.user.email)
        self.assertFalse(form.fields["contact"].required)

    def test_the_page_field_is_prefilled_from_where_the_person_came_from(self):
        response = self.client.get(reverse("support"), headers={"referer": "https://knt-mipt.ru/library/12/"})
        self.assertEqual(response.context["form"].initial["page"], "https://knt-mipt.ru/library/12/")

    def test_the_support_page_itself_is_not_offered_as_the_broken_one(self):
        # После отправки браузер шлёт referer со страницы поддержки — подставлять его нельзя.
        response = self.client.get(reverse("support"), headers={"referer": "https://knt-mipt.ru/support/"})
        self.assertEqual(response.context["form"].initial["page"], "")

    def test_the_message_carries_who_wrote_and_how_to_answer(self):
        text = render_to_string("telegram/support.html", {
            "author": self.user, "topic": "Предложение", "text": "Добавьте тёмную тему", "page": "", "contact": "@ivan",
        })
        self.assertIn("Добавьте тёмную тему", text)
        self.assertIn("Иван Петров", text)
        self.assertIn(self.user.email, text)
        self.assertIn("@ivan", text)
        self.assertNotIn("Страница:", text)


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
            "page": "", "contact": "ivan@mipt.ru",
        })
        self.assertIn("не вошёл на сайт", text)
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
