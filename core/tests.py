import json
from pathlib import Path

from django.core import mail
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
