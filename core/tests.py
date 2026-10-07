import copy
import re
import subprocess
import tempfile
from datetime import date, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from smtplib import SMTPServerDisconnected
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.mail import EmailMessage, EmailMultiAlternatives, send_mail
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import ResolverMatch, get_resolver, reverse
from django.utils import timezone

import yaml
from PIL import Image as PilImage

from attachments.storage import file_storage
from attachments.uploads import under
from knt.celery import app as celery_app
from core.models import Team
from teachers.models import Review, Teacher
from users.models import User

from chats.models import Chat

from . import backup, nav
from .search import by_name
from .throttle import client_ip, throttled
from .markup import render
from .mail import pack
from .tasks import MAIL_RETRIES, backup_database, ping, send_email


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


class MailFailureTests(TestCase):
    """Письмо, которое так и не ушло, не пропадает молча: 4 октября 2026 ящик сайта
    исчез из Workspace, и узнали об этом от людей, оставшихся без сброса пароля."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        # Повторы на месте идут только без propagates: с ним наружу летит сам Retry.
        celery_app.conf.task_eager_propagates = False
        self.addCleanup(setattr, celery_app.conf, "task_eager_propagates", True)

    def send(self, outcome, to="student@t.local"):
        """Письмо, доставка которого кончается outcome: сколько было попыток, что ушло боту
        и чем кончилась задача."""
        letter = EmailMultiAlternatives("Сброс пароля", "Ссылка: https://knt.local/reset/abc/", to=[to])
        with patch("core.tasks.deliver", side_effect=outcome) as deliver, \
             patch("telegram.notify.send_message") as bot:
            result = send_email.apply(args=[pack(letter)])
        return SimpleNamespace(
            attempts=deliver.call_count, sent=[call.args for call in bot.delay.call_args_list], error=result.result,
        )

    def test_a_letter_that_never_left_is_reported_once_the_attempts_are_over(self):
        with self.assertLogs("core.mail", "ERROR"):
            letter = self.send(OSError("сеть лежит"))

        self.assertEqual(letter.attempts, MAIL_RETRIES + 1)
        self.assertEqual(len(letter.sent), 1)
        chat, text = letter.sent[0]
        self.assertEqual(chat, "support")
        for part in ("Сброс пароля", "student@t.local", "сеть лежит"):
            self.assertIn(part, text)

    def test_the_body_stays_out_of_the_chat(self):
        # В теле — ссылка, по которой задают пароль.
        with self.assertLogs("core.mail", "ERROR"):
            text = self.send(OSError("сеть лежит")).sent[0][1]

        self.assertNotIn("reset/abc", text)

    def test_a_letter_that_left_on_a_retry_is_not_reported(self):
        with self.assertNoLogs("core.mail", "ERROR"):
            letter = self.send([OSError("моргнуло"), None])

        self.assertEqual(letter.attempts, 2)
        self.assertEqual(letter.sent, [])

    def test_an_error_nobody_retries_is_reported_at_once(self):
        with self.assertLogs("core.mail", "ERROR"):
            letter = self.send(ValueError("письмо собрано криво"))

        self.assertEqual(letter.attempts, 1)
        self.assertEqual(len(letter.sent), 1)

    def test_the_chat_hears_about_the_first_failure_of_the_hour_and_the_log_about_each(self):
        # Почта ломается вся разом: ведомость курса дала бы сотню одинаковых сообщений.
        with self.assertLogs("core.mail", "ERROR") as log:
            first = self.send(OSError("сеть лежит"), to="one@t.local")
            second = self.send(OSError("сеть лежит"), to="two@t.local")

        self.assertEqual((len(first.sent), len(second.sent)), (1, 0))
        self.assertIn("one@t.local", log.output[0])
        self.assertIn("two@t.local", log.output[1])

    def test_a_dropped_connection_comes_with_a_hint_about_the_password(self):
        # Так у Gmail выглядит отказ во входе: настоящий ответ 535 smtplib теряет.
        with self.assertLogs("core.mail", "ERROR"):
            dropped = self.send(SMTPServerDisconnected("Connection unexpectedly closed")).sent[0][1]
            cache.clear()
            other = self.send(OSError("сеть лежит")).sent[0][1]

        self.assertIn("отказ в логине или пароле", dropped)
        self.assertNotIn("отказ в логине или пароле", other)

    def test_a_broken_alert_does_not_hide_the_mail_failure(self):
        letter = EmailMultiAlternatives("Сброс пароля", "Текст", to=["student@t.local"])
        with patch("core.tasks.deliver", side_effect=OSError("сеть лежит")), \
             patch("telegram.notify.send_message") as bot, \
             self.assertLogs("core.mail", "ERROR") as log:
            bot.delay.side_effect = RuntimeError("очередь сломана")
            result = send_email.apply(args=[pack(letter)])

        self.assertIsInstance(result.result, OSError)
        self.assertIn("Не получилось сообщить", log.output[-1])


class DeployTests(SimpleTestCase):
    """Раскладка контейнеров: то, что ломается молча и видно только на выкладке."""

    APP = ("web", "worker", "beat", "bot")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        text = (settings.BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        cls.services = yaml.safe_load(text)["services"]

    def test_nothing_of_ours_starts_before_the_migrations_are_done(self):
        """Иначе новый код встречается со старой схемой: задача, читающая только что
        добавленную колонку, в это окно падает."""
        self.assertEqual(self.services["migrate"]["entrypoint"], ["python", "manage.py", "migrate", "--noinput"])
        for name in self.APP:
            with self.subTest(name):
                self.assertEqual(
                    self.services[name]["depends_on"]["migrate"], {"condition": "service_completed_successfully"},
                )

    def test_a_failed_migration_is_not_run_in_a_loop(self):
        self.assertEqual(self.services["migrate"]["restart"], "no")

    def test_the_site_does_not_migrate_on_its_own_any_more(self):
        # Двое на одной схеме разом только подрались бы.
        entry = (settings.BASE_DIR / "entrypoint.sh").read_text(encoding="utf-8")

        self.assertNotIn("manage.py migrate", entry)
        self.assertIn("manage.py collectstatic", entry)

    def test_the_image_is_built_once_and_shared(self):
        self.assertEqual([name for name, one in self.services.items() if "build" in one], ["migrate"])
        for name in self.APP:
            with self.subTest(name):
                self.assertEqual(self.services[name]["image"], self.services["migrate"]["image"])
                # Образ свой, в реестре его нет: без этого compose пошёл бы его скачивать.
                self.assertEqual(self.services[name]["pull_policy"], "never")

    def test_the_image_carries_a_postgres_client_of_the_servers_version(self):
        """Им ночью снимается бэкап (core/backup.py). Клиент младше сервера дамп снять
        откажется, и узнать об этом было бы не от кого."""
        server = self.services["db"]["image"].partition(":")[2].partition(".")[0]
        dockerfile = (settings.BASE_DIR / "Dockerfile").read_text(encoding="utf-8")

        self.assertTrue(server.isdigit(), server)
        self.assertIn(f" postgresql-client-{server} ", dockerfile)

    def test_redis_has_a_ceiling_and_keeps_the_task_queue_out_of_eviction(self):
        """У очереди Celery срока жизни нет — вытеснять можно только то, у чего он есть."""
        command = self.services["redis"]["command"]

        self.assertEqual(command[command.index("--maxmemory") + 1], "256mb")
        self.assertEqual(command[command.index("--maxmemory-policy") + 1], "volatile-lru")

    def test_the_deploy_action_is_pinned_by_commit(self):
        """Тег автор действия может перевесить на другой код, а шаг держит ключ от сервера."""
        workflow = (settings.BASE_DIR / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        used = re.findall(r"^\s*uses:\s*(\S+)", workflow, re.M)

        self.assertTrue(used)
        for action in used:
            self.assertRegex(action, r"@[0-9a-f]{40}$")


class BackupTests(TestCase):
    """Ночной бэкап базы: дамп в хранилище под `backups/` и уборка старых."""

    TODAY = date(2026, 10, 7)  # среда

    def setUp(self):
        self.storage = file_storage()
        self.wipe()
        self.addCleanup(self.wipe)  # каталог хранилища один на весь прогон

    def wipe(self):
        for key in under("backups"):
            self.storage.delete(key)

    def dumping(self, body=b"PGDMP-dump", code=0, stderr="", **database):
        """Подменить pg_dump: настоящий в тестах звать не на чем. Что ему передали — в self.asked."""
        def run(command, env, **kwargs):
            self.asked = SimpleNamespace(command=command, env=env)
            if not code:
                target = next(arg for arg in command if arg.startswith("--file="))
                Path(target.removeprefix("--file=")).write_bytes(body)
            return subprocess.CompletedProcess(command, code, "", stderr)

        told = {"NAME": "knt", "USER": "site", "PASSWORD": "s3cret", "HOST": "db", "PORT": "", **database}
        base = patch("core.backup.connection", SimpleNamespace(vendor="postgresql", settings_dict=told))
        tool = patch("core.backup.subprocess.run", side_effect=run)
        self.addCleanup(base.stop)
        self.addCleanup(tool.stop)
        base.start()
        return tool.start()

    def put(self, *days):
        for day in days:
            self.storage.save(backup.key_for(day), ContentFile(b"old"))

    def days_back(self, count):
        return [self.TODAY - timedelta(days=n) for n in range(count)]

    def test_the_dump_lands_in_the_storage_under_todays_date(self):
        self.dumping(b"PGDMP-today")

        key, size = backup.make()

        self.assertEqual(key, f"backups/knt-{timezone.localdate():%Y-%m-%d}.dump")
        self.assertEqual(size, len(b"PGDMP-today"))
        with self.storage.open(key) as stored:
            self.assertEqual(stored.read(), b"PGDMP-today")

    def test_the_whole_database_is_asked_for_in_the_format_one_can_restore_from(self):
        self.dumping()

        backup.make()

        command = self.asked.command
        self.assertEqual((command[0], command[-1]), ("pg_dump", "knt"))
        for part in ("--format=custom", "--username=site", "--host=db", "--port=5432"):
            self.assertIn(part, command)

    def test_the_password_goes_by_the_environment_and_not_by_the_arguments(self):
        # Аргументы процесса видны всякому, кто смотрит список процессов.
        self.dumping()

        backup.make()

        self.assertEqual(self.asked.env["PGPASSWORD"], "s3cret")
        self.assertNotIn("s3cret", " ".join(self.asked.command))

    def test_a_local_socket_gets_no_host_argument(self):
        self.dumping(HOST="", PORT="5433")

        backup.make()

        self.assertFalse([part for part in self.asked.command if part.startswith("--host")])
        self.assertIn("--port=5433", self.asked.command)

    def test_a_second_run_the_same_day_replaces_the_first(self):
        self.dumping(b"first")
        backup.make()
        self.dumping(b"second, longer")

        key, _ = backup.make()

        self.assertEqual(list(under("backups")), [key])
        with self.storage.open(key) as stored:
            self.assertEqual(stored.read(), b"second, longer")

    def test_a_failed_dump_stores_nothing_and_says_why(self):
        self.dumping(code=1, stderr="pg_dump: error: connection to server failed")

        with self.assertRaisesMessage(backup.BackupError, "connection to server failed"):
            backup.make()

        self.assertEqual(list(under("backups")), [])

    def test_only_postgres_is_backed_up(self):
        tool = self.dumping()

        with patch("core.backup.connection", SimpleNamespace(vendor="sqlite", settings_dict={})), \
             self.assertRaisesMessage(backup.BackupError, "только с Postgres"):
            backup.make()

        tool.assert_not_called()

    def test_two_weeks_of_days_and_two_months_of_sundays_are_kept(self):
        days = self.days_back(120)
        self.put(*days)

        gone = backup.prune()

        sundays = [day for day in days if day.isoweekday() == 7]
        kept = set(days[:backup.KEEP_DAILY]) | set(sundays[:backup.KEEP_WEEKLY])
        self.assertEqual(set(backup.stored()), kept)
        self.assertEqual(len(kept), backup.KEEP_DAILY + backup.KEEP_WEEKLY - 2)  # два воскресенья уже среди дневных
        self.assertEqual(len(gone), 120 - len(kept))

    def test_a_missed_night_does_not_eat_an_older_backup(self):
        """Считаем по тому, что лежит, а не по календарю: пять дампов за полгода — все пять на месте."""
        rare = [self.TODAY - timedelta(days=n) for n in (1, 30, 60, 100, 170)]
        self.put(*rare)

        self.assertEqual(backup.prune(), [])
        self.assertEqual(set(backup.stored()), set(rare))

    def test_nothing_but_our_own_dumps_is_touched(self):
        self.put(*self.days_back(40))
        foreign = ["backups/заметка.txt", "backups/knt-2020-01-01.dump.part", "backups/2020/knt-2020-01-01.dump",
                   "books/knt-2020-01-01.dump"]
        for key in foreign:
            self.storage.save(key, ContentFile(b"not ours"))
        self.addCleanup(self.storage.delete, "books/knt-2020-01-01.dump")

        backup.prune()

        for key in foreign:
            self.assertTrue(self.storage.exists(key), key)
        self.assertNotIn(date(2020, 1, 1), backup.stored())

    def test_the_nightly_task_makes_a_dump_and_drops_the_old_ones(self):
        self.put(*[timezone.localdate() - timedelta(days=n) for n in range(1, 40)])
        self.dumping(b"PGDMP-night")

        answer = backup_database()

        today = backup.key_for(timezone.localdate())
        self.assertIn(f"{today}: {len(b'PGDMP-night')} байт", answer)
        self.assertNotIn("старых снято: 0", answer)
        self.assertTrue(self.storage.exists(today))
        self.assertLessEqual(len(backup.stored()), backup.KEEP_DAILY + backup.KEEP_WEEKLY)

    def test_the_schedule_runs_it_every_night(self):
        entry = settings.CELERY_BEAT_SCHEDULE["backup-database"]

        self.assertEqual(entry["task"], backup_database.name)
        self.assertIn(entry["task"], celery_app.tasks)
        self.assertEqual((entry["schedule"].hour, entry["schedule"].minute), ({3}, {40}))
        self.assertEqual(len(entry["schedule"].day_of_week), 7)

    def run_command(self, *args):
        out = StringIO()
        call_command("backup", *args, stdout=out)
        return out.getvalue()

    def test_the_command_lists_what_is_stored_without_making_a_new_one(self):
        self.put(self.TODAY, self.TODAY - timedelta(days=1))
        tool = self.dumping()

        listing = self.run_command("--list")

        tool.assert_not_called()
        self.assertLess(listing.index("2026-10-07"), listing.index("2026-10-06"))  # свежие сверху
        self.assertIn("всего: 2", listing)

    def test_the_command_makes_a_backup_by_hand(self):
        self.dumping(b"PGDMP-hand")

        answer = self.run_command()

        self.assertIn(f"снято: {backup.key_for(timezone.localdate())}", answer)
        self.assertIn("всего: 1", answer)

    def test_the_command_reports_a_failed_dump_as_its_own_error(self):
        self.dumping(code=1, stderr="pg_dump: error: server version mismatch")

        with self.assertRaisesMessage(CommandError, "server version mismatch"):
            self.run_command()

    def fetch(self, which):
        screen = SimpleNamespace(buffer=BytesIO())
        with patch("core.management.commands.backup.sys.stdout", screen):
            call_command("backup", "--fetch", which, stdout=StringIO())
        return screen.buffer.getvalue()

    def test_the_command_hands_out_a_dump_byte_for_byte(self):
        """Ради восстановления: дамп двоичный, и текстовый вывод команды его бы испортил."""
        raw = b"PGDMP\x00\x01\xff\r\n\x80 binary"
        self.put(self.TODAY - timedelta(days=3))
        self.storage.save(backup.key_for(self.TODAY), ContentFile(raw))

        self.assertEqual(self.fetch("latest"), raw)
        self.assertEqual(self.fetch("2026-10-07"), raw)
        self.assertEqual(self.fetch("2026-10-04"), b"old")

    def test_asking_for_a_backup_that_is_not_there_names_what_is(self):
        self.put(self.TODAY)

        with self.assertRaisesMessage(CommandError, "Есть: 2026-10-07"):
            self.fetch("2026-01-01")
        self.wipe()
        with self.assertRaisesMessage(CommandError, "ни одного"):
            self.fetch("latest")


class DevCommandTests(TestCase):
    """Сиды помечены «только для разработки», но на боевой базе запускались бы как любые:
    `seed_chats` завела бы там переписку между настоящими людьми."""

    SEEDS = ("seed_books", "seed_materials", "seed_chats")

    def run_it(self, name):
        # --wipe — самый дешёвый путь по команде: убирать в пустой базе нечего.
        user = make_user(f"{name}@t.local")
        extra = ["--user", user.email] if name == "seed_chats" else []
        call_command(name, "--wipe", *extra, stdout=StringIO())

    def test_outside_development_a_seed_refuses_to_run(self):
        for name in self.SEEDS:
            with self.subTest(name), self.assertRaisesMessage(CommandError, "только для разработки"):
                self.run_it(name)

    @override_settings(DEBUG=True)
    def test_in_development_it_runs_as_before(self):
        self.run_it("seed_books")
        self.run_it("seed_materials")
        # У переписки отказ свой — в пустой базе не из кого собрать собеседников; до него надо дойти.
        with self.assertRaisesMessage(CommandError, "слишком мало людей"):
            self.run_it("seed_chats")


@override_settings(ALLOWED_HOSTS=["knt-mipt.ru", "files.inbicst.ru"])
class MarkupImageTests(SimpleTestCase):
    """Картинка в тексте грузится у читателя сама — с чужого адреса она сообщает
    своему хозяину, кто и когда открыл материал."""

    def test_a_foreign_picture_becomes_a_link_to_it(self):
        html = render("до ![схема](https://evil.example/pixel.png) после")

        self.assertNotIn("<img", html)
        self.assertIn('<a href="https://evil.example/pixel.png"', html)
        self.assertIn(">схема</a> после", html)

    def test_without_a_caption_the_link_shows_the_address(self):
        self.assertIn(">https://evil.example/p.png</a>", render("![](https://evil.example/p.png)"))

    def test_our_own_pictures_stay_pictures(self):
        for url in ("/static/core/img/logo.png", "https://knt-mipt.ru/static/x.png", "https://files.inbicst.ru/img/x"):
            with self.subTest(url):
                self.assertIn(f'src="{url}"', render(f"![наша]({url})"))

    def test_an_address_that_only_looks_like_ours_is_foreign(self):
        lookalikes = (
            "https://knt-mipt.ru@evil.example/x.png", "https://knt-mipt.ru.evil.example/x.png",
            "//evil.example/x.png", "/\\evil.example/x.png", "ftp://knt-mipt.ru/x.png",
        )
        for url in lookalikes:
            with self.subTest(url):
                self.assertNotIn("<img", render(f"![обман]({url})"))

    def test_a_picture_written_as_raw_html_loses_its_address(self):
        # Такая в дерево markdown не попадает — её ловит уже чистка готового HTML.
        html = render('<img src="https://evil.example/raw.png" alt="сырая">')

        self.assertNotIn("evil.example", html)

    def test_a_tab_hidden_in_the_address_does_not_help(self):
        # Браузер табы из адреса выкидывает, и «/ + таб + /host» становится «//host».
        self.assertNotIn("src=", render('<img src="/&#9;/evil.example/x.png">'))

    def test_a_raw_picture_of_ours_keeps_its_address(self):
        self.assertIn('src="/static/ok.png"', render('<img src="/static/ok.png">'))

    def test_a_foreign_picture_inside_a_link_leaves_the_link_whole(self):
        html = render("[![кнопка](https://evil.example/in.png)](https://site.example/page)")

        self.assertNotIn("evil.example", html)
        self.assertIn('<a href="https://site.example/page"', html)
        self.assertIn("кнопка</span></a>", html)


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


class OpenSectionsTests(TestCase):
    """Сайт открыт целиком: ни одного раздела за замком.

    Класс остался от беты, когда геймификация была закрыта до готовности кейсов и значков.
    Замок снят к релизу, и проверка развернулась: теперь она следит, чтобы ни один раздел
    не оказался закрыт снова по недосмотру — молча погасший пункт меню заметить трудно.
    """

    def setUp(self):
        self.reader = make_user("reader@x.ru")
        self.client.force_login(self.reader)

    def test_every_section_is_reachable(self):
        # Загрузка и правка тоже, а не только чтение. Стены тут нет: без заведённой доски
        # она честно отвечает 404, и это про доску, а не про доступ.
        for name in ("material_list", "book_list", "teacher_list", "support", "chat_list",
                     "material_new", "book_new", "playlist_list", "shop"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_the_profile_and_what_hangs_off_it_are_open(self):
        self.assertEqual(self.client.get(reverse("profile", args=[self.reader.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("profile_edit")).status_code, 200)

    def test_an_anonymous_visitor_is_sent_to_login(self):
        self.client.logout()
        target = reverse("profile", args=[self.reader.pk])

        self.assertRedirects(self.client.get(target), f"{reverse('login')}?next={target}")

    def test_the_root_leads_to_materials(self):
        self.assertRedirects(self.client.get("/"), reverse("material_list"))

    def test_the_menu_gives_out_every_link(self):
        """Пункт без href — это раздел, который кто-то закрыл и забыл открыть обратно."""
        page = self.client.get(reverse("material_list")).content.decode()

        for name in ("playlist_list", "shop", "wall", "book_list"):
            self.assertIn(f'href="{reverse(name)}"', page, name)
        self.assertIn(reverse("profile", args=[self.reader.pk]), page)

    def test_nothing_promises_a_beta_any_more(self):
        page = self.client.get(reverse("material_list")).content.decode()

        self.assertNotIn("бета", page.lower())


class ApplicantsPageTests(TestCase):
    """Единственная страница, открытая всему интернету, и меню, которое видит гость."""

    def page(self):
        return self.client.get(reverse("applicants"))

    def test_a_guest_lands_on_it_from_the_root(self):
        """Раньше корень уводил гостя на форму входа. На knt-mipt.ru приходят и те,
        кто сюда только собирается поступать, — им нужна витрина, а не замок."""
        self.assertRedirects(self.client.get("/"), reverse("applicants"))

    def test_a_student_still_lands_in_materials(self):
        self.client.force_login(make_user("reader@x.ru"))

        self.assertRedirects(self.client.get("/"), reverse("material_list"))

    def test_it_opens_without_logging_in(self):
        self.assertEqual(self.page().status_code, 200)

    def test_the_answers_came_over_from_the_old_site(self):
        page = self.page().content.decode()

        self.assertIn("Абитуриентам о КНТ", page)
        self.assertIn("34 бюджетных и 5 платных мест", page)   # Поступление
        self.assertIn("военной кафедрой", page)                # Обучение
        self.assertIn("Есть ли у вас столовая?", page)         # Кампус

    def test_a_guest_gets_three_menu_items(self):
        page = self.page().content.decode()
        menu = page.split('<nav')[1].split('</nav>')[0]

        self.assertIn(f'href="{reverse("applicants")}"', menu)
        self.assertIn(f'href="{reverse("material_list")}"', menu)
        self.assertIn(f'href="{reverse("contacts")}"', menu)
        # Разделов сайта в меню гостя нет вовсе: за каждым всё равно форма входа.
        for hidden in ("shop", "wall", "chat_list", "book_list", "student_list"):
            self.assertNotIn(reverse(hidden), menu, hidden)

    def test_the_section_lights_up(self):
        self.assertEqual(self.page().context["section"], "applicants")

    def test_a_guest_gets_no_sidebar_at_all(self):
        """Три пункта в колонке шириной в 256 пикселей — и целая шапка ради кнопки темы."""
        guest = self.page().content.decode()
        self.client.force_login(make_user("reader@x.ru"))
        student = self.client.get(reverse("material_list")).content.decode()

        self.assertNotIn("<aside", guest)
        self.assertIn("<aside", student)

    def test_the_footer_leads_here(self):
        """«О факультете» в подвале — эта самая страница; раньше там стояла решётка."""
        for page in (self.page(), self.client.get(reverse("support"))):
            footer = page.content.decode().split("<footer")[1]

            self.assertIn(f'href="{reverse("applicants")}"', footer)
            self.assertNotIn('href="#"', footer)

    def test_a_student_gets_the_usual_menu_and_no_applicants_item(self):
        """Иначе пункт для абитуриентов болтался бы в меню у тех, кто давно поступил."""
        self.client.force_login(make_user("reader@x.ru"))

        page = self.client.get(reverse("material_list")).content.decode()
        menu = page.split('<nav')[1].split('</nav>')[0]

        self.assertIn(f'href="{reverse("shop")}"', menu)
        self.assertNotIn(reverse("applicants"), menu)

    def test_the_address_answer_leads_to_the_contacts_section(self):
        """Со старого сайта этот ответ и вёл в «Контакты» — там телефоны и схема проезда."""
        page = self.page().content.decode()

        self.assertIn("улица Максимова, дом 4", page)
        self.assertIn(f'href="{reverse("contacts")}"', page)


class ContactsPageTests(TestCase):
    """Вторая страница, открытая всему интернету: адрес, люди и форма вопроса."""

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты

    def page(self):
        return self.client.get(reverse("contacts"))

    def test_it_opens_without_logging_in(self):
        self.assertEqual(self.page().status_code, 200)

    def test_everything_came_over_from_the_old_site(self):
        page = self.page().content.decode()

        self.assertIn("ул. Максимова, 4", page)
        self.assertIn("Давтян Александр Георгиевич", page)      # деканат
        self.assertIn("tel:+74991965311", page)
        self.assertIn("Актуальный состав студсовета", page)     # студсовет
        self.assertIn("Приходько Дарья", page)                  # общежития
        self.assertIn("mailto:khmelevskii.aa@mipt.ru", page)    # обучение
        self.assertIn("Попов Алексей Васильевич", page)         # глава студсовета
        self.assertIn("mailto:knt.student.council@gmail.com", page)

    def test_the_section_lights_up(self):
        self.assertEqual(self.page().context["section"], "contacts")

    def test_a_question_reaches_the_feedback_chat_and_not_support(self):
        """Разные чаты не для порядка: про поступление и про сломанный сайт отвечают разные люди."""
        with patch("core.views.notify") as sent:
            response = self.client.post(reverse("contacts"), {
                "text": "До какого числа подают документы?",
                "name": "Абитуриент", "contact": "abit@mail.ru",
            })

        self.assertRedirects(response, reverse("contacts"))
        chat, template, context = sent.call_args.args
        self.assertEqual(chat, "feedback")
        self.assertEqual(template, "telegram/feedback.html")
        self.assertIsNone(context["author"])
        self.assertEqual(context["contact"], "abit@mail.ru")

    def test_without_a_name_and_a_contact_there_is_nowhere_to_answer(self):
        with patch("core.views.notify") as sent:
            response = self.client.post(reverse("contacts"), {"text": "Вопрос"})

        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "contact", "Обязательное поле.")
        sent.assert_not_called()

    def test_a_logged_in_person_is_asked_for_neither(self):
        """И имя, и связь есть в профиле — ссылка на него уезжает в чат."""
        self.client.force_login(make_user("reader@x.ru"))

        fields = self.page().context["form"].fields
        self.assertNotIn("name", fields)
        self.assertNotIn("contact", fields)

    def test_a_flood_stops_reaching_the_chat(self):
        payload = {"text": "спам", "name": "спам", "contact": "spam@x.ru"}
        with patch("core.views.notify") as sent:
            for _ in range(14):
                self.client.post(reverse("contacts"), payload)

        self.assertEqual(sent.call_count, 10)

    def test_the_message_says_who_asked_and_how_to_answer(self):
        text = render_to_string("telegram/feedback.html", {
            "author": None, "text": "Есть ли общежитие?", "name": "Абитуриент", "contact": "abit@mail.ru",
        })

        self.assertIn("Есть ли общежитие?", text)
        self.assertIn("Абитуриент", text)
        self.assertIn("abit@mail.ru", text)


class TeacherSectionTests(TestCase):
    """Раздел преподавателей целиком — не только чтение, но и отзывы."""

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

    def test_a_made_up_forwarded_address_does_not_reset_the_limit(self):
        """Счёт шёл по первому адресу из X-Forwarded-For, а его пишет сам клиент:
        новый заголовок на каждый запрос — и ограничителя нет."""
        payload = {"topic": "other", "text": "спам", "contact": "spam@x.ru"}
        with patch("core.views.notify") as sent:
            for number in range(14):
                self.client.post(reverse("support"), payload, headers={"x-forwarded-for": f"10.1.1.{number}"})
        self.assertEqual(sent.call_count, 10)


class ClientIpTests(SimpleTestCase):
    """Чей это запрос. Верим только адресу, который видел наш nginx."""

    def ip(self, **meta):
        return client_ip(RequestFactory().get("/", **meta))

    def test_the_address_is_the_one_nginx_saw(self):
        self.assertEqual(self.ip(HTTP_X_REAL_IP="203.0.113.5", REMOTE_ADDR="172.18.0.1"), "203.0.113.5")
        self.assertEqual(self.ip(HTTP_X_REAL_IP="2001:db8::1", REMOTE_ADDR="172.18.0.1"), "2001:db8::1")

    def test_what_the_client_says_about_itself_is_not_asked(self):
        seen = self.ip(HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.5", HTTP_X_REAL_IP="203.0.113.5")
        self.assertEqual(seen, "203.0.113.5")

    def test_without_nginx_it_is_the_address_of_the_connection(self):
        self.assertEqual(self.ip(HTTP_X_FORWARDED_FOR="1.2.3.4", REMOTE_ADDR="10.0.0.7"), "10.0.0.7")

    def test_what_is_not_an_address_is_nothing(self):
        """Строка отсюда едет в поле адреса сессии: мусор там — это пятисотка на входе."""
        self.assertEqual(self.ip(HTTP_X_REAL_IP="unknown", REMOTE_ADDR="10.0.0.7"), "10.0.0.7")
        self.assertEqual(self.ip(HTTP_X_REAL_IP="unknown", REMOTE_ADDR="тоже не адрес"), "")


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


class NavSectionTests(SimpleTestCase):
    """Пункт меню подсвечен по РАЗДЕЛУ, а не по странице: уйдя со списка материалов
    в конкретный материал, человек из раздела не вышел."""

    def at(self, url_name):
        request = RequestFactory().get("/")
        request.resolver_match = ResolverMatch(lambda r: None, (), {}, url_name=url_name)
        return nav.section(request)

    def test_inner_pages_keep_their_section_lit(self):
        self.assertEqual(self.at("material_list"), "materials")
        self.assertEqual(self.at("material_detail"), "materials")
        self.assertEqual(self.at("playlist_detail"), "lectorium")
        self.assertEqual(self.at("book_edit"), "library")

    def test_a_page_outside_the_menu_lights_nothing(self):
        # Профиля и поддержки в меню нет — подсвечивать нечего, и это не ошибка.
        self.assertEqual(self.at("profile"), "")
        self.assertEqual(self.at(None), "")

    def test_every_name_in_the_map_is_a_real_url(self):
        """Карта живёт отдельно от urls.py: переименовали урл — пункт молча погаснет
        навсегда, и заметить это можно только глазами."""
        known = get_resolver().reverse_dict
        missing = sorted(name for names in nav.SECTIONS.values() for name in names if name not in known)

        self.assertFalse(missing, f"в core/nav.py имена, которых нет среди урлов: {missing}")


class StaticBuildTests(SimpleTestCase):
    """Статика собирается ровно так, как её собирает боевой контейнер.

    В разработке `{% static %}` отдаёт файл как есть, а на бою staticfiles лежит на
    whitenoise.CompressedManifestStaticFilesStorage: тот дописывает к именам хеш и ради
    этого ЧИТАЕТ каждый css и js, переписывая ссылки внутри. Ссылка в никуда — и сборка
    падает целиком, то есть контейнер не поднимается вовсе.

    На это уже наступили: у скачанной hls.min.js в хвосте стояло
    `//# sourceMappingURL=hls.min.js.map`, а карты рядом не было, — деплой лёг, хотя
    и тесты, и разработка проходили. Дешевле собрать статику в тестах (пара секунд),
    чем узнавать об этом с боевого домена.
    """

    def test_collectstatic_survives_the_production_storage(self):
        storages = copy.deepcopy(settings.STORAGES)
        storages["staticfiles"] = {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}
        with tempfile.TemporaryDirectory() as room:
            with override_settings(STORAGES=storages, STATIC_ROOT=room, DEBUG=False):
                call_command("collectstatic", "--noinput", verbosity=0)


class FooterTests(TestCase):
    """Подвал и его ссылки.

    У списков конца нет: подвал под ними недостижим, пока не догрузишь весь каталог,
    а до тех пор он успевает мелькнуть на каждой подгрузке. Поэтому там его нет вовсе,
    а ссылки переехали в меню аккаунта — оттуда они доступны с любой страницы и не стоят
    боковой панели ни пикселя высоты.
    """

    # Страницы с бесконечной лентой. Закладок тут нет намеренно: они показываются
    # целиком, без подгрузки, и подвал под ними достижим.
    ENDLESS = ["material_list", "book_list", "playlist_list", "teacher_list", "student_list", "wallet"]

    def setUp(self):
        self.client.force_login(make_user("reader@t.local"))

    def test_the_endless_lists_have_no_footer(self):
        for name in self.ENDLESS:
            with self.subTest(page=name):
                self.assertNotContains(self.client.get(reverse(name)), "<footer")

    def test_every_link_of_the_footer_is_reachable_from_an_endless_list(self):
        """Ради этого всё и затевалось: с ленты материалов до поддержки было не добраться,
        не догрузив весь каталог."""
        page = self.client.get(reverse("material_list"))

        for name in ["applicants", "contacts", "support"]:
            with self.subTest(link=name):
                self.assertContains(page, f'href="{reverse(name)}"')
        self.assertContains(page, "vk.com/knt_mipt")

    def test_a_page_that_ends_keeps_its_footer(self):
        self.assertContains(self.client.get(reverse("bookmark_list")), "<footer")

    def test_a_guest_keeps_the_footer_he_has_nowhere_else_to_go_from(self):
        """Боковой панели у гостя нет, и ссылки ему больше взять неоткуда."""
        self.client.logout()

        self.assertContains(self.client.get(reverse("applicants")), "<footer")
