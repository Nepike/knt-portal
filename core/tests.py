import copy
import importlib
import json
import re
import subprocess
import tempfile
import time
from datetime import date, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from smtplib import SMTPServerDisconnected
from types import SimpleNamespace
from unittest.mock import patch

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.models import Session
from django.contrib.staticfiles import finders
from django.core import mail, signing
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.mail import EmailMessage, EmailMultiAlternatives, send_mail
from django.db import OperationalError
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import ResolverMatch, get_resolver, path, reverse
from django.utils import timezone

import yaml
from PIL import Image as PilImage

from attachments.models import human_size
from attachments.storage import file_storage
from attachments.uploads import under
from knt import urls as site_urls
from knt.celery import app as celery_app
from core.models import Failure, Funding, Inquiry, Lockdown, Supporter, Team
from intake import urls as intake_urls
from printing import urls as printing_urls
from teachers.models import Review, Teacher
from users.models import LoginEvent, User, UserSession

from chats.consumers import ChatConsumer
from chats.models import Chat
from wall.models import Board

from . import backup, donors, lockdown, nav, runs, spam
from .context_processors import theme_for
from .search import by_name
from .failures import record
from .forms import MAX_SUPPORT_IMAGE, FeedbackForm, StampedForm, SupportForm
from .middleware import SLOW, SWOLLEN, SlowRequestMiddleware
from .throttle import client_ip, exhausted, forgive, strike, throttled
from .markup import render
from .mail import pack
from .tasks import INQUIRY_DAYS, JOURNAL_DAYS, MAIL_RETRIES, backup_database, ping, send_email, sweep_journals


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

    def test_a_letter_that_never_left_lands_in_the_failure_journal_once(self):
        """Один раз: задача письма падает и попадает в общий обработчик, но пишет о себе
        сама — у неё есть адресат и тема."""
        with self.assertLogs("core.mail", "ERROR"):
            self.send(OSError("сеть лежит"))

        failure = Failure.objects.get()
        self.assertEqual(failure.source, Failure.Source.MAIL)
        for part in ("student@t.local", "Сброс пароля", "сеть лежит"):
            self.assertIn(part, failure.text)
        self.assertNotIn("reset/abc", failure.text)

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

    def test_web_workers_are_renewed_and_do_not_hoard_memory(self):
        """Воркер web за три дня дорастал до ~580 МБ на машине с 1,9 ГБ (замер 7 ноя 2026),
        и 3 ноя оба встали разом. Свежий воркер на смену отслужившему и две арены glibc."""
        dockerfile = (settings.BASE_DIR / "Dockerfile").read_text(encoding="utf-8")
        command = json.loads(re.search(r"^CMD (\[.*\])$", dockerfile, re.M).group(1))

        self.assertIn("--max-requests", command)
        self.assertIn("--max-requests-jitter", command)
        self.assertRegex(dockerfile, r"(?m)^ENV .*\bMALLOC_ARENA_MAX=2\b")

    def test_the_deploy_action_is_pinned_by_commit(self):
        """Тег автор действия может перевесить на другой код, а шаг держит ключ от сервера."""
        used = []
        for workflow in (settings.BASE_DIR / ".github" / "workflows").glob("*.yml"):
            # И `uses:` вторым ключом шага, и `- uses:` первым.
            used += re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", workflow.read_text(encoding="utf-8"), re.M)

        self.assertTrue(used)
        for action in used:
            self.assertRegex(action, r"@[0-9a-f]{40}$")

    def test_the_proxy_password_stays_out_of_the_build_log(self):
        """Строку RUN сборка печатает с подставленными аргументами, даже взятую из кэша. Так адрес
        прокси с паролем три месяца лежал в логе каждого деплоя, а лог Actions открытого
        репозитория читает любой вошедший на GitHub. Предопределённые аргументы Docker доходят
        до команды окружением и в лог не попадают — но только если их не объявить через ARG."""
        dockerfile = (settings.BASE_DIR / "Dockerfile").read_text(encoding="utf-8")
        args = self.services["migrate"]["build"]["args"]
        predefined = {"HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "NO_PROXY", "ALL_PROXY"}

        self.assertIn("HTTPS_PROXY", args)
        for name in args:
            if "PROXY" in name.upper():
                self.assertIn(name.upper(), predefined)
        self.assertNotRegex(dockerfile, r"(?im)^\s*ARG\s+\w*PROXY")

    def test_a_deploy_waits_for_the_new_version_to_answer(self):
        """`up -d` рапортует успех, даже если web тут же уходит в цикл перезапусков."""
        workflow = yaml.safe_load(
            (settings.BASE_DIR / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        )
        script = next(
            step["with"]["script"] for step in workflow["jobs"]["deploy"]["steps"] if "script" in step.get("with", {})
        )
        lines = [line.strip() for line in script.splitlines()]

        up = lines.index("docker compose up -d --build")
        check = next(i for i, line in enumerate(lines) if line.startswith("sh tools/wait_healthy.sh "))
        self.assertGreater(check, up)
        self.assertTrue((settings.BASE_DIR / "tools" / "wait_healthy.sh").is_file())

    def test_the_monthly_selftest_is_walled_off_from_production(self):
        """Тесты в боевом образе на боевом сервере (tools/selftest.sh): с файлом окружения
        сервера или в сети compose они дотянулись бы до боевой базы, бакета и чатов."""
        script = (settings.BASE_DIR / "tools" / "selftest.sh").read_text(encoding="utf-8")

        self.assertNotIn("--env-file", script)
        self.assertNotIn("env_file", script)
        # Своя сеть, а не сеть compose (knt_default): там по имени db живёт боевая база.
        net = re.search(r"(?m)^net=(\S+)$", script).group(1)
        self.assertFalse(net.startswith("knt_"), net)
        self.assertEqual(set(re.findall(r"--network\s+(\S+)", script)), {'"$net"'})
        self.assertIn("-e R2_BUCKET= ", script)
        self.assertIn("DJANGO_SETTINGS_MODULE=knt.settings.dev", script)
        # Та же версия Postgres, что у боевой базы: иначе прогон проверял бы не то.
        self.assertIn(f" {self.services['db']['image']} ", script)


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

    def test_an_address_of_ours_that_is_not_a_picture_becomes_a_link(self):
        """`![](/f/…)` у каждого читателя «скачивал» бы файл автора, а скачивания приносят
        токены: накрутка без единого настоящего скачивания."""
        for url in ("/f/abc/конспект.pdf", "https://files.inbicst.ru/f/abc/x.pdf",
                    "https://knt-mipt.ru/hls/abc/seg.m4s", "/materials/1/"):
            with self.subTest(url):
                html = render(f"![файл]({url})")
                self.assertNotIn("<img", html)
                self.assertIn(">файл</a>", html)

    def test_dots_in_the_path_do_not_lead_out_of_the_pictures(self):
        """Браузер схлопывает «..» и «%2e%2e», и /img/../f/… у него стал бы /f/…."""
        for url in ("/img/../f/abc/x.pdf", "/img/%2e%2e/f/abc/x.pdf", "/static/./../f/x",
                    "https://files.inbicst.ru/img/%2E%2E/f/x"):
            with self.subTest(url):
                self.assertNotIn("<img", render(f"![обход]({url})"))

    def test_a_raw_picture_of_ours_that_is_not_a_picture_loses_its_address(self):
        self.assertNotIn("src=", render('<img src="/f/abc/x.pdf">'))

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


def oversized_upload():
    """Файл на байт больше потолка открытых форм. Содержимое неважно: до разбора дойти не должно."""
    return SimpleUploadedFile("x.gif", b"\0" * (MAX_SUPPORT_IMAGE + 1), content_type="image/gif")


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


class SlowRequestTests(SimpleTestCase):
    """Строка в лог о запросе, который шёл долго или раздул память воркера, — и только о таком."""

    def call(self, spent=0.1, peaks=(100_000, 100_000)):
        middleware = SlowRequestMiddleware(lambda request: HttpResponse(status=201))
        request = RequestFactory().get("/materials/")
        request.user = SimpleNamespace(pk=7)
        with patch("core.middleware.time.monotonic", side_effect=[10.0, 10.0 + spent]), \
                patch("core.middleware.peak_kb", side_effect=list(peaks)):
            return middleware(request)

    def test_a_slow_request_is_written_down(self):
        with self.assertLogs("knt.slow", "WARNING") as log:
            self.assertEqual(self.call(spent=SLOW + 0.5).status_code, 201)

        self.assertIn("GET /materials/ → 201 за 2.5 с", log.output[0])
        self.assertIn("человек 7", log.output[0])

    def test_a_request_that_swelled_the_worker_is_written_down(self):
        with self.assertLogs("knt.slow", "WARNING") as log:
            self.call(peaks=(100_000, 100_000 + SWOLLEN + 1024))

        self.assertIn("пик памяти +21 МБ", log.output[0])

    def test_an_ordinary_request_leaves_no_trace(self):
        with self.assertNoLogs("knt.slow"):
            self.call(spent=SLOW - 0.5, peaks=(100_000, 100_000 + SWOLLEN - 1))

    def test_without_a_memory_meter_only_time_counts(self):
        """На windows модуля resource нет — разработка не падает и не пишет пустых строк."""
        with self.assertNoLogs("knt.slow"):
            self.call(peaks=(None, None))


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


class AboutPageTests(TestCase):
    """«О сайте» — единственная страница, открытая всему интернету, и меню, которое видит гость."""

    def page(self):
        return self.client.get(reverse("about"))

    def test_a_guest_lands_on_it_from_the_root(self):
        """Раньше корень уводил гостя на форму входа. На knt-mipt.ru приходят и те,
        у кого аккаунта нет, — им нужна витрина, а не замок."""
        self.assertRedirects(self.client.get("/"), reverse("about"))

    def test_a_student_still_lands_in_materials(self):
        self.client.force_login(make_user("reader@x.ru"))

        self.assertRedirects(self.client.get("/"), reverse("material_list"))

    def test_it_opens_without_logging_in(self):
        self.assertEqual(self.page().status_code, 200)

    def test_it_says_who_keeps_the_site_and_the_applicants_page_is_gone(self):
        """Страницу для абитуриентов сняли 15 окт 2026, когда факультет отказался оплачивать
        сайт. Шаблон лежит на случай возврата, но наружу из него не видно ничего."""
        page = self.page().content.decode()

        self.assertIn("Сайт разрабатывают, поддерживают и спонсируют студенты", page)
        self.assertNotIn("Абитуриентам", page)
        self.assertNotIn("34 бюджетных", page)
        self.assertRedirects(self.client.get("/applicants/"), reverse("about"), status_code=301)

    @override_settings(SITE_THEME="halloween")
    def test_the_title_keeps_plain_letters_under_a_skin(self):
        """Потёки хэллоуина на странице благодарностей не к месту: у заголовка нет крючка скина."""
        page = self.page().content.decode()

        self.assertIn('class="theme-halloween"', page)
        self.assertRegex(page, r'<h1 class="(?![^"]*page-title)[^"]*">Сайт разрабатывают')

    def test_a_guest_gets_three_menu_items(self):
        page = self.page().content.decode()
        menu = page.split('<nav')[1].split('</nav>')[0]

        self.assertIn(f'href="{reverse("about")}"', menu)
        self.assertIn('class="top-link top-link-active">О сайте<', menu)
        self.assertIn(f'href="{reverse("material_list")}"', menu)
        self.assertIn(f'href="{reverse("contacts")}"', menu)
        # Разделов сайта в меню гостя нет вовсе: за каждым всё равно форма входа.
        for hidden in ("shop", "wall", "chat_list", "book_list", "student_list"):
            self.assertNotIn(reverse(hidden), menu, hidden)

    def test_the_section_lights_up(self):
        self.assertEqual(self.page().context["section"], "about")

    def test_a_guest_gets_no_sidebar_at_all(self):
        """Три пункта в колонке шириной в 256 пикселей — и целая шапка ради кнопки темы."""
        guest = self.page().content.decode()
        self.client.force_login(make_user("reader@x.ru"))
        student = self.client.get(reverse("material_list")).content.decode()

        self.assertNotIn("<aside", guest)
        self.assertIn("<aside", student)

    def test_the_footer_and_the_account_menu_lead_here(self):
        """«О сайте» в подвале — эта самая страница; раньше там стояла решётка."""
        for page in (self.page(), self.client.get(reverse("support"))):
            footer = page.content.decode().split("<footer")[1]

            self.assertIn(f'href="{reverse("about")}"', footer)
            self.assertIn(">О сайте<", footer)
            self.assertNotIn('href="#"', footer)

        self.client.force_login(make_user("reader@x.ru"))
        self.assertContains(self.client.get(reverse("bookmark_list")), "</i>О сайте</a>")

    def test_a_student_gets_the_usual_menu_and_no_about_item(self):
        """Верхнее меню у вошедшего — разделы сайта; «О сайте» у него в меню аккаунта."""
        self.client.force_login(make_user("reader@x.ru"))

        page = self.client.get(reverse("material_list")).content.decode()
        menu = page.split('<nav')[1].split('</nav>')[0]

        self.assertIn(f'href="{reverse("shop")}"', menu)
        self.assertNotIn(reverse("about"), menu)


# Выгрузка донатов Boosty в том виде, в каком её отдаёт сайт. Люди выдуманы.
EXPORT = (
    "name;email;type;price;total;date;target_description\n"
    '"Иван Петров";petrov@example.org;donation;200,00;176,60;2026-10-15;"Хостинг"\n'
    '"иван  петров";petrov@example.org;donation;100,00;88,30;2026-10-16;"Хостинг"\n'
    '"Иван Пахомов";;donation;300,00;264,90;2026-10-14;\n'
    '"Котик";kot@example.org;donation;5000,00;4415,00;2026-10-15;"Хостинг"\n'
    '"Тест Тестов";test@example.org;donation;4000,00;3532,00;2026-10-13;\n'
    '"Ёжик Сидоров";;subscription;100,00;88,30;2026-10-13;\n'
    '"Алёна Сидорова";;donation;100,00;88,30;2026-10-13;\n'
)
SIGNED = ["Алёна С.", "Иван Па.", "Иван Пе.", "Котик"]


class DonorsTests(SimpleTestCase):
    """Выгрузка Boosty → подписи для страницы (core/donors.py)."""

    def test_only_donations_are_read_and_the_bom_does_not_stick_to_the_header(self):
        names = donors.read(EXPORT.encode("utf-8-sig"))

        self.assertEqual(names, ["Иван Петров", "иван  петров", "Иван Пахомов", "Котик", "Тест Тестов", "Алёна Сидорова"])

    def test_a_file_without_names_is_not_an_export(self):
        with self.assertRaises(ValueError):
            donors.read("a;b\n1;2\n".encode())

    def test_each_person_once_the_skipped_out_surnames_cut_alphabetical(self):
        """Повтор узнаётся без учёта регистра и пробелов; «ё» в сортировке — как «е»."""
        names = donors.read(EXPORT.encode("utf-8-sig"))

        self.assertEqual(donors.signatures(names, ["тест тестов"]), SIGNED)
        self.assertEqual(donors.signatures(["Яна", "Ёлка"]), ["Ёлка", "Яна"])

    def test_namesakes_get_letters_until_they_differ(self):
        names = ["Елена Меньшикова", "Елена Михеева", "Елена Михайлова"]

        self.assertEqual(donors.signatures(names), ["Елена Ме.", "Елена Миха.", "Елена Михе."])

    def test_whom_a_letter_cannot_tell_apart_is_written_in_full(self):
        """Одна фамилия, разные отчества, или одна — начало другой: сокращать нечем.
        Разошлись только на последней букве — тоже целиком, без точки."""
        self.assertEqual(donors.signatures(["Анна Петрова", "Анна Мария Петрова"]), ["Анна Мария Петрова", "Анна Петрова"])
        self.assertEqual(donors.signatures(["Олег Кимов", "Олег Ким"]), ["Олег Ким", "Олег Кимов"])
        self.assertEqual(donors.signatures(["Ян Лиа", "Ян Либ"]), ["Ян Лиа", "Ян Либ"])


class FundingTests(TestCase):
    """Сбор на странице «О сайте»: месяцы слева, чек со спонсорами справа."""

    TODAY = date(2026, 10, 15)

    def setUp(self):
        today = patch("django.utils.timezone.localdate", return_value=self.TODAY)
        today.start()
        self.addCleanup(today.stop)

    def fund(self, since, until, *names, **fields):
        funding = Funding.objects.create(since=since, until=until, **fields)
        for name in names:
            funding.supporters.create(name=name)
        return funding

    def page(self):
        return self.client.get(reverse("about")).content.decode()

    def test_without_names_there_is_neither_receipt_nor_months(self):
        self.fund(date(2026, 10, 1), date(2027, 1, 1))

        page = self.page()

        self.assertIn("Сайт разрабатывают, поддерживают и спонсируют студенты", page)
        self.assertNotIn("ЧЕК №", page)
        self.assertNotIn("Месяцы работы сайта", page)

    def test_the_receipt_has_the_period_the_count_and_the_people_alphabetically(self):
        funding = self.fund(date(2026, 10, 1), date(2027, 1, 1), "Яромир Т.", "Котик", "Алёна С.", loaded=self.TODAY)

        page = self.page()

        self.assertIn(f"ЧЕК № {funding.pk:04d}", page)
        self.assertIn("<span>15.10.2026</span>", page)
        self.assertIn("<span>окт 2026 – янв 2027</span>", page)
        self.assertRegex(page, r"Спонсоров</span><span[^>]*></span><span>3</span>")
        self.assertLess(page.index(">Алёна С.<"), page.index(">Котик<"))
        self.assertLess(page.index(">Котик<"), page.index(">Яромир Т.<"))

    def test_the_names_are_escaped(self):
        self.fund(date(2026, 10, 1), date(2027, 1, 1), "Котик <b>")

        self.assertIn(">Котик &lt;b&gt;<", self.page())

    def test_the_months_are_the_funding_and_two_more(self):
        funding = self.fund(date(2026, 10, 1), date(2027, 1, 1), "Котик")

        self.assertEqual([(month["day"], month["paid"], month["now"]) for month in funding.months()], [
            (date(2026, 10, 1), True, True), (date(2026, 11, 1), True, False), (date(2026, 12, 1), True, False),
            (date(2027, 1, 1), True, False), (date(2027, 2, 1), False, False), (date(2027, 3, 1), False, False),
        ])
        page = self.page()
        self.assertIn("Сейчас он оплачен по январь 2027 года включительно", page)
        tiles = page.split('aria-label="Месяцы работы сайта"')[1].split("</ol>")[0]
        for month in ("Октябрь", "Январь", "Март"):
            self.assertIn(f">{month}<", tiles)
        self.assertNotIn("Апрель", tiles)
        self.assertEqual(tiles.count("пока не оплачен"), 2)
        self.assertEqual(tiles.count(">сейчас<"), 1)

    def test_a_funding_that_is_over_stays_in_the_past_until_the_next_one(self):
        """Первого числа страница не пустеет: пока нового сбора нет, висит прошлый."""
        self.fund(date(2026, 7, 1), date(2026, 9, 1), "Летний")

        page = self.page()
        self.assertIn("Сайт был оплачен по сентябрь 2026 года", page)
        self.assertIn(">Летний<", page)

        self.fund(date(2026, 10, 1), date(2027, 1, 1), "Осенний")

        page = self.page()
        self.assertIn(">Осенний<", page)
        self.assertNotIn("Летний", page)

    def test_the_last_month_of_a_funding_is_still_now(self):
        self.fund(date(2026, 8, 1), date(2026, 10, 1), "Котик")

        self.assertIn("Сейчас он оплачен по октябрь 2026 года включительно", self.page())

    def test_a_funding_ahead_waits_for_its_turn(self):
        self.fund(date(2026, 11, 1), date(2027, 3, 1), "Заранее")

        self.assertNotIn("Заранее", self.page())

    def test_any_day_counts_as_its_month_and_the_end_is_not_before_the_start(self):
        funding = self.fund(date(2026, 10, 29), date(2027, 1, 31))

        self.assertEqual((funding.since, funding.until), (date(2026, 10, 1), date(2027, 1, 1)))
        with self.assertRaises(ValidationError):
            Funding(since=date(2026, 10, 1), until=date(2026, 9, 30)).clean()
        Funding(since=date(2026, 10, 20), until=date(2026, 10, 5)).clean()  # один и тот же месяц — можно


class FundingAdminTests(TestCase):
    """Выгрузка Boosty — прямо в форме сбора: имена на сайте меняются сами."""

    TODAY = date(2026, 10, 15)

    def setUp(self):
        today = patch("django.utils.timezone.localdate", return_value=self.TODAY)
        today.start()
        self.addCleanup(today.stop)
        self.client.force_login(User.objects.create_superuser("boss@t.local", "Анна", "Главная", password="pass12345"))

    def send(self, url, export=None, **fields):
        data = {
            "since": "2026-10-01", "until": "2027-01-01", "skip": "Тест Тестов",
            "supporters-TOTAL_FORMS": "0", "supporters-INITIAL_FORMS": "0",
            "supporters-MIN_NUM_FORMS": "0", "supporters-MAX_NUM_FORMS": "1000", **fields,
        }
        if export is not None:
            data["export"] = SimpleUploadedFile("stat-donations.csv", export)
        return self.client.post(url, data)

    def test_the_export_turns_into_signatures_and_nothing_else(self):
        self.send(reverse("admin:core_funding_add"), EXPORT.encode("utf-8-sig"))

        funding = Funding.objects.get()
        self.assertEqual(funding.names(), SIGNED)
        self.assertEqual(funding.loaded, self.TODAY)
        # Ни почт, ни сумм: в базе лежат только подписи.
        self.assertNotIn("@", " ".join(Supporter.objects.values_list("name", flat=True)))
        self.assertContains(self.client.get(reverse("about")), ">Котик<")

    def test_a_new_export_replaces_the_names(self):
        self.send(reverse("admin:core_funding_add"), EXPORT.encode("utf-8-sig"))
        funding = Funding.objects.get()

        later = EXPORT + '"Новый Человек";;donation;100,00;88,30;2026-10-20;\n'
        self.send(reverse("admin:core_funding_change", args=[funding.pk]), later.encode("utf-8-sig"))

        self.assertEqual(funding.names(), ["Алёна С.", "Иван Па.", "Иван Пе.", "Котик", "Новый Ч."])

    def test_without_a_file_the_names_stay(self):
        self.send(reverse("admin:core_funding_add"), EXPORT.encode("utf-8-sig"))
        funding = Funding.objects.get()

        self.send(reverse("admin:core_funding_change", args=[funding.pk]), until="2027-02-01")

        funding.refresh_from_db()
        self.assertEqual(funding.until, date(2027, 2, 1))
        self.assertEqual(funding.names(), SIGNED)
        # Подпись можно поправить и руками — список имён стоит прямо в форме сбора.
        self.assertContains(self.client.get(reverse("admin:core_funding_change", args=[funding.pk])), 'name="supporters-0-name"')

    def test_a_file_that_is_not_an_export_saves_nothing(self):
        response = self.send(reverse("admin:core_funding_add"), "просто текст".encode("cp1251"))
        self.assertContains(response, "Файл не в UTF-8")

        response = self.send(reverse("admin:core_funding_add"), b"a;b\n1;2\n")
        self.assertContains(response, "это не выгрузка донатов Boosty")

        self.assertFalse(Funding.objects.exists())


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

    # Деканат — как на странице дирекции на mipt.ru: человек, должность, телефон, почта.
    DEANERY = [
        ("Акимова Екатерина Александровна", "Заместитель директора по учебной работе", "+74991968970", "akimova.ea@mipt.ru"),
        ("Правоторова Яна Витальевна", "Заместитель директора", "+74991968970", "pravotorova@mipt.ru"),
        ("Давтян Александр Георгиевич", "Руководитель направления (учебный блок)", "+74991965311", None),
        ("Михалева Ольга Владимировна", "Руководитель направления", "+74991964974", "mikhaleva.ov@mipt.ru"),
        ("Гагиева Ирина Евгеньевна", "Специалист по методической работе", "+74991965311", "gagieva_2013@mail.ru"),
    ]

    def deanery(self):
        """Карточки деканата по порядку: каждая — кусок разметки одного человека."""
        section = self.page().content.decode().split("Деканат</h2>")[1].split("Студсовет</h2>")[0]
        return re.findall(r'(?s)<div class="panel p-6.*?</div>', section)

    def test_the_deans_office_is_listed_as_on_the_university_page(self):
        """У каждого своя должность, свой телефон и своя почта — и стоят они в том же порядке."""
        cards = self.deanery()

        self.assertEqual(len(cards), len(self.DEANERY))
        for card, (name, title, phone, email) in zip(cards, self.DEANERY):
            with self.subTest(person=name):
                self.assertIn(f">{name}<", card)
                self.assertIn(f">{title}<", card)
                self.assertIn(f'href="tel:{phone}"', card)
                self.assertEqual(re.findall(r'href="mailto:([^"]+)"', card), [email] if email else [])

    def test_the_old_titles_are_gone(self):
        page = self.page().content.decode()

        self.assertNotIn("Руководитель учебного блока", page)
        self.assertNotIn("Руководитель административного блока", page)

    def test_everyone_has_a_photo_that_is_really_there_but_the_one_without(self):
        """У кого фотографии нет, тому инициалы — а не битая картинка и не чужое лицо."""
        *with_photo, without = self.deanery()

        for card in with_photo:
            name, = re.findall(r'src="/static/(core/img/[\w-]+\.jpg)"', card)
            with self.subTest(photo=name):
                self.assertIsNotNone(finders.find(name))
        self.assertEqual(len({re.search(r'src="([^"]+)"', card).group(1) for card in with_photo}), len(with_photo))
        self.assertNotIn("<img", without)
        self.assertIn(">ГИ<", without)

    def test_the_section_lights_up(self):
        self.assertEqual(self.page().context["section"], "contacts")

    def test_a_question_reaches_the_feedback_chat_and_not_support(self):
        """Разные чаты не для порядка: про поступление и про сломанный сайт отвечают разные люди."""
        with patch("core.inquiries.notify") as sent:
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
        with patch("core.inquiries.notify") as sent:
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
        with patch("core.inquiries.notify") as sent:
            for _ in range(14):
                self.client.post(reverse("contacts"), payload)

        self.assertEqual(sent.call_count, 10)

    def test_a_picture_over_the_limit_is_turned_away_before_pillow_opens_it(self):
        """Разбор картинки не бесплатен, а форма открыта всем (отчёт 30 окт 2026)."""
        with patch("PIL.Image.open") as opened:
            response = self.client.post(reverse("contacts"), {"text": "?", "image": oversized_upload()})

        self.assertFormError(response.context["form"], "image", f"Картинка больше {human_size(MAX_SUPPORT_IMAGE)}")
        opened.assert_not_called()

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
        with patch("core.inquiries.notify") as sent:
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
        with patch("core.inquiries.notify") as sent:
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
        with patch("core.inquiries.notify") as sent:
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
        with patch("core.inquiries.notify") as sent:
            response = self.client.post(reverse("support"), {"topic": "account", "text": "Не приходит письмо"})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "contact", "Обязательное поле.")
        sent.assert_not_called()

    def test_a_visitor_with_a_contact_gets_through(self):
        with patch("core.inquiries.notify") as sent:
            response = self.client.post(reverse("support"), {
                "topic": "account", "text": "Не приходит письмо", "contact": "ivan@mipt.ru",
            })
        self.assertRedirects(response, reverse("support"))
        context = sent.call_args.args[2]
        self.assertIsNone(context["author"])
        self.assertEqual(context["contact"], "ivan@mipt.ru")

    def test_a_picture_over_the_limit_is_turned_away_before_pillow_opens_it(self):
        """Разбор картинки не бесплатен, а форма открыта всем (отчёт 30 окт 2026)."""
        with patch("PIL.Image.open") as opened:
            response = self.client.post(reverse("support"), {"topic": "other", "text": "?", "image": oversized_upload()})

        self.assertFormError(response.context["form"], "image", f"Картинка больше {human_size(MAX_SUPPORT_IMAGE)}")
        opened.assert_not_called()

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
        with patch("core.inquiries.notify") as sent:
            for _ in range(14):
                self.client.post(reverse("support"), payload)
        self.assertEqual(sent.call_count, 10)

    def test_a_made_up_forwarded_address_does_not_reset_the_limit(self):
        """Счёт шёл по первому адресу из X-Forwarded-For, а его пишет сам клиент:
        новый заголовок на каждый запрос — и ограничителя нет."""
        payload = {"topic": "other", "text": "спам", "contact": "spam@x.ru"}
        with patch("core.inquiries.notify") as sent:
            for number in range(14):
                self.client.post(reverse("support"), payload, headers={"x-forwarded-for": f"10.1.1.{number}"})
        self.assertEqual(sent.call_count, 10)


def stamp(ago):
    """Метка формы, открытой `ago` секунд назад."""
    return signing.dumps(time.time() - ago, salt=StampedForm.SALT)


class SpamScoreTests(SimpleTestCase):
    """Штрафные баллы гостю (core/spam.py): каждый признак отдельно и примеры целиком."""

    def score(self, text, name="", contact="", seconds=40):
        return spam.score(text, name, contact, seconds)

    def signs(self, *args, **kwargs):
        return dict(self.score(*args, **kwargs)[1])

    def test_a_plain_question_scores_nothing(self):
        self.assertEqual(self.score("Есть ли общежитие?"), (0, []))

    def test_markup_alone_is_enough(self):
        """Живой человек в простом поле не пишет <a href> — так пишет рассыльщик под форум."""
        self.assertEqual(self.signs("<b>B2B-Lab</b> — лаборатория")["HTML-разметка"], 20)
        self.assertIn("HTML-разметка", self.signs("[url=http://x.ru]тут[/url]"))
        self.assertGreaterEqual(self.score("<a href=https://x.ru>кухни</a>")[0], spam.THRESHOLD)

    def test_links_to_other_sites_add_up_to_a_ceiling(self):
        self.assertEqual(self.signs("Смотри https://example.com/page"), {"чужая ссылка": 6})
        self.assertEqual(self.signs("https://a.ru и https://b.ru"), {"чужих ссылок: 2": 9})
        self.assertEqual(self.signs("вот https://a.ru https://b.ru https://c.ru https://d.ru"), {"чужих ссылок: 4": 12})

    def test_a_link_without_https_is_still_a_link(self):
        """«psee.io/8rkbf9» телеграм сам превращает в ссылку — значит, и мы её видим."""
        self.assertEqual(self.signs("Жми сюда psee.io/8rkbf9"), {"чужая ссылка": 6, "сокращённая ссылка": 4})

    def test_our_own_places_are_not_suspicious(self):
        text = "Смотри https://knt-mipt.ru/lectures/31/ и https://mipt.ru/education/, пиши в vk.com/knt_mipt или t.me/ivan, олимпиады — olimpiada.ru"
        self.assertEqual(self.score(text), (0, []))

    def test_an_address_or_an_abbreviation_is_not_a_link(self):
        self.assertEqual(self.score("Пишите на ivan.petrov@mail.ru, т.е. в г.Долгопрудный к 9.30"), (0, []))

    def test_latin_letters_inside_russian_words(self):
        """«Здравcтвyйте» с латинскими c и y обходит фильтры по словам; «PDFка» так пишут живые."""
        self.assertEqual(self.signs("Здравcтвyйте, у нас скидки"), {"латинские буквы внутри русских слов": 8})
        self.assertEqual(self.score("Не открывается PDFка, и ITшник не помог"), (0, []))

    def test_no_russian_at_all(self):
        self.assertEqual(self.signs("Hello"), {"ни одной русской буквы": 4})

    def test_a_dictionary_group_counts_once_and_groups_add_up(self):
        self.assertEqual(self.signs("Продвижение сайта и SEO, продвижение бизнеса"), {"словарь: реклама и SEO": 8})
        self.assertEqual(self.score("SEO и казино")[0], 16)
        self.assertEqual(self.score("SEO, казино, биткоин и НДС")[0], 16)

    def test_what_students_and_applicants_say_is_not_in_the_dictionary(self):
        for text in (
            "Ваш сайт не работает, на вашем сайте не открываются билеты",
            "Где посмотреть посещаемость лекций и продвинутый курс по криптографии?",
            "Куда отправить чеки об оплате общежития?",
            "Извините за спам, пишу третий раз: не приходит письмо",
            "Как отписаться от рассылки сайта?",
            "Через VPN и Tor сайт не открывается, есть зеркало?",
            "Есть ли образовательный кредит и сколько стоит обучение?",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.score(text), (0, []))
        self.assertEqual(self.signs("I study electron spin resonance and want to invest my time in physics"),
                         {"ни одной русской буквы": 4})

    def test_a_phone_in_the_text(self):
        self.assertEqual(self.signs("Перезвоните: +7 916 123-45-67"), {"телефон в тексте": 2})

    def test_a_name_from_a_mailing_robot(self):
        for name in ("JosephWeism", "RobertoGeorm", "Robert77", "XRumer23Slife", "GermanSlife"):
            with self.subTest(name=name):
                self.assertEqual(len(self.signs("Вопрос", name=name)), 1)
                self.assertEqual(self.score("Вопрос", name=name)[0], 5)
        # Одно имя латиницей — так пишет и иностранный абитуриент.
        for name in ("Alexander", "Markus", "Иван", "Елена Викторовна", "Sara Merritt"):
            with self.subTest(name=name):
                self.assertEqual(self.score("Вопрос", name=name), (0, []))

    def test_where_to_answer(self):
        cases = {
            "ivan@mail.ru": 1,
            "anna.ivanova2008@mail.ru": 1,
            "k.eithy2art.e.rbe.rr@gmail.com": 4,     # точки gmail — один ящик на сотню адресов
            "alinaall232323@gmail.com": 4,
            "@ivan_p": -5,
            "тг evelaveta": -5,
            "Telegram: @mathphy": -5,
            "vk.com/id1": -5,
            "Pgkvs Lzw Bh": 4,
            "KW": 4,
            "Australia": 0,
            "+79161234567": 0,
            "": 0,
        }
        for contact, points in cases.items():
            with self.subTest(contact=contact):
                self.assertEqual(self.score("Вопрос", contact=contact)[0], points)

    def test_how_fast_the_form_was_filled(self):
        self.assertEqual(self.signs("Вопрос", seconds=None), {"нет метки времени формы": 6})
        self.assertEqual(self.signs("Вопрос", seconds=2), {"отправлено за 2 с": 6})
        self.assertEqual(self.score("Вопрос", seconds=spam.FAST), (0, []))

    # Присланные в ноябре 2026 — пересказом, с выдуманными контактами
    SPAM = [
        ("Экономическая эффективность — главный аргумент в пользу аутсорсинга https://лдбух.рф/page1.html "
         "Содержание штатного бухгалтера https://лдбух.рф/poster", "RobertoGeorm", "liza.rudneva.2023@mail.ru"),
        ("Мы предлагаем авточехлы на заказ https://интерьер-авто52.рф/product/a/ "
         "https://интерьер-авто52.рф/product/b/", "Brothers", "x@mail.ru"),
        ("Your last spin could change everything. Why risk waiting?! Play Now Here => psee.io/8rkbf9",
         "Leighsh", "rohanuy49@zohomail.eu"),
        ("Hey there, this note came in through your contact form. Test it yourself: https://ultraleadgeneration.com "
         "Not for you? Opt out here: https://ultraleadgeneration.com/unsubscribe", "Sara Merritt", "Australia"),
        ('Drug dosage <a href=""> https://outreachhs.org/ativan-online.php </a>', "Dennisvon", "k.eithy@gmail.com"),
        ("<a href=https://wheelouttogame.online>белорусские кухни в москве</a>", "JosephWeism", "d@wheelouttogame.online"),
        ("While looking into a <a href=https://haircolorideas.top>hair color simulator</a> I started", "Ethanneab", "g@x.com"),
        ("webbankir займ на карту <a href=https://creditmain.ru/>Микрозаймы онлайн</a>", "Aleksandrbag", "alex1@yandex.ru"),
        ("Доброго времени суток. <b>B2B-Lab</b> — лаборатория цифровых продуктов", "Wesleygreft", "w@gmx.com"),
        ("Ready to spin, bet, and win? With https://lkqs.cc/403d21 you're not just betting", "Josephalite", "f@gmail.com"),
        ("Feeling confident about your picks? Cement that feeling with 1Win! https://lkqs.cc/403d21", "Kellycoarf", "f@gmail.com"),
    ]
    GUESTS = [
        ("Здравствуйте! Подскажите, какие проходные баллы были на КНТ и дают ли общежитие?", "Анна", "anna.ivanova2008@mail.ru"),
        ("Сын хочет поступать к вам. Перезвоните, пожалуйста: +7 916 123-45-67", "Елена Викторовна", "+79161234567"),
        ("Hello! I am an international applicant from Kazakhstan. Is there a dormitory?", "Alexander", "alex.smith@gmail.com"),
        ("Ne mogu voiti na sait, parol ne podhodit", "", "nikita2007@mail.ru"),
        ("Тут написано другое: https://mipt.ru/education/ — кому верить?", "Олег", "oleg@mail.ru"),
        ("Не открываются файлы, скрин: https://disk.yandex.ru/i/abc", "Маша", "masha@yandex.ru"),
        ("Подскажите, когда день открытых дверей?", "Лиза", "тг evelaveta"),
    ]

    def test_the_spam_that_came_in_november_is_stopped(self):
        for text, name, contact in self.SPAM:
            with self.subTest(text=text[:40]):
                self.assertGreaterEqual(self.score(text, name, contact)[0], spam.THRESHOLD)

    def test_people_who_write_for_real_get_through(self):
        for text, name, contact in self.GUESTS:
            with self.subTest(text=text[:40]):
                self.assertLess(self.score(text, name, contact)[0], spam.THRESHOLD)


class StampedFormTests(SimpleTestCase):
    """Сколько заполняли форму: подписанная метка в скрытом поле, без скриптов."""

    def test_a_fresh_form_carries_a_signed_stamp(self):
        html = str(FeedbackForm(known=True)["started"])

        self.assertIn('type="hidden"', html)
        value = re.search(r'value="([^"]+)"', html).group(1)
        self.assertLessEqual(FeedbackForm({"started": value}, known=True).seconds(), 1)

    def test_seconds_run_from_the_opening(self):
        self.assertEqual(SupportForm({"started": stamp(40)}).seconds(), 40)

    def test_a_missing_or_foreign_stamp_is_none(self):
        for value in (None, "", "1731150000", signing.dumps(time.time() - 40, salt="чужая"), stamp(40) + "x"):
            with self.subTest(value=value):
                data = {} if value is None else {"started": value}
                self.assertIsNone(FeedbackForm(data, known=True).seconds())

    def test_a_stamp_from_the_future_is_zero(self):
        self.assertEqual(FeedbackForm({"started": stamp(-30)}, known=True).seconds(), 0)

    def test_the_stamp_survives_a_form_with_errors(self):
        """Вернулась форма с ошибкой — время считается от первого открытия, а не заново."""
        value = stamp(40)
        form = FeedbackForm({"started": value, "text": ""})

        self.assertFalse(form.is_valid())
        self.assertIn(f'value="{value}"', str(form["started"]))


class InquiryTests(TestCase):
    """Обращения из поддержки и контактов: каждое в таблице, гостя проверяют баллы."""

    def setUp(self):
        cache.clear()  # ограничитель частоты живёт в кэше и переживает тесты

    def ask(self, text="Есть ли общежитие?", ago=40, page="contacts", **extra):
        data = {"text": text, "started": stamp(ago), "name": "Аня", "contact": "anna@mail.ru", "topic": "account", **extra}
        with patch("core.inquiries.notify") as sent:
            response = self.client.post(reverse(page), data)
        self.assertRedirects(response, reverse(page))
        return sent

    def test_both_forms_carry_the_stamp(self):
        for page in ("contacts", "support"):
            with self.subTest(page=page):
                self.assertContains(self.client.get(reverse(page)), 'name="started"')

    def test_a_question_from_a_guest_is_written_down_and_sent(self):
        sent = self.ask()

        inquiry = Inquiry.objects.get()
        self.assertEqual(inquiry.verdict, Inquiry.Verdict.SENT)
        self.assertEqual(inquiry.source, Inquiry.Source.CONTACTS)
        self.assertEqual((inquiry.name, inquiry.contact, inquiry.text), ("Аня", "anna@mail.ru", "Есть ли общежитие?"))
        self.assertEqual((inquiry.score, inquiry.reasons), (1, [["почта для ответа", 1]]))
        self.assertIn(inquiry.seconds, (40, 41))
        self.assertEqual(inquiry.ip, "127.0.0.1")
        chat, template, context = sent.call_args.args
        self.assertEqual((chat, template), ("feedback", "telegram/feedback.html"))
        self.assertEqual(context["inquiry"], inquiry)

    def test_spam_from_a_guest_is_kept_but_goes_nowhere(self):
        """И бот видит то же «Спасибо»: подсказывать, на чём он попался, незачем."""
        with patch("core.inquiries.notify") as sent:
            response = self.client.post(reverse("contacts"), {
                "text": "<a href=https://x.ru>кухни</a>", "name": "JosephWeism", "contact": "d@x.ru", "started": stamp(40),
            }, follow=True)

        sent.assert_not_called()
        self.assertContains(response, "Спасибо, вопрос ушёл")
        inquiry = Inquiry.objects.get()
        self.assertEqual(inquiry.verdict, Inquiry.Verdict.SPAM)
        self.assertEqual(inquiry.score, 32)
        self.assertIn(["HTML-разметка", 20], inquiry.reasons)

    def test_the_same_text_from_a_logged_in_person_is_not_checked(self):
        self.client.force_login(make_user("reader@x.ru"))

        sent = self.ask("<b>срочно</b> https://x.ru")

        sent.assert_called_once()
        inquiry = Inquiry.objects.get()
        self.assertEqual((inquiry.verdict, inquiry.score, inquiry.reasons), (Inquiry.Verdict.SENT, 0, []))
        self.assertIsNotNone(inquiry.author)

    def test_a_form_sent_too_fast_tips_the_scale(self):
        text = "Не открываются файлы, скрин: https://disk.yandex.ru/i/abc"

        self.ask(text, ago=40).assert_called_once()
        self.ask(text, ago=1).assert_not_called()

        self.assertEqual(list(Inquiry.objects.order_by("created").values_list("score", "verdict")),
                         [(7, "sent"), (13, "spam")])

    def test_exactly_the_threshold_is_already_stopped(self):
        """Одна группа словаря и почта для ответа: 8 + 1 = 9."""
        self.ask("Предлагаем продвижение сайта недорого").assert_not_called()

        self.assertEqual(Inquiry.objects.get().score, spam.THRESHOLD)

    def test_support_from_a_guest_is_checked_too(self):
        self.ask("Купить SEO-продвижение сайта недорого", page="support", contact="baza@gmail.com", ago=1).assert_not_called()

        inquiry = Inquiry.objects.get()
        self.assertEqual((inquiry.source, inquiry.topic, inquiry.verdict), ("support", "account", "spam"))

    def test_the_picture_of_a_caught_message_goes_nowhere(self):
        self.ask("<b>Скидки</b>", image=make_image()).assert_not_called()

        self.assertTrue(Inquiry.objects.get().had_image)

    def test_the_chat_sees_the_points_of_a_guest(self):
        sent = self.ask("Не открываются файлы, скрин: https://disk.yandex.ru/i/abc")

        text = render_to_string(*sent.call_args.args[1:])
        self.assertIn("Баллы: 7 — чужая ссылка +6, почта для ответа +1", text)

    def test_a_logged_in_message_has_no_points_line(self):
        self.client.force_login(make_user("reader@x.ru"))

        sent = self.ask("Не открывается книга", page="support")

        self.assertNotIn("Баллы", render_to_string(*sent.call_args.args[1:]))

    def test_over_the_limit_nothing_is_even_written_down(self):
        """Бот, долбящий с одного адреса, иначе завалил бы таблицу строками."""
        with patch("core.inquiries.notify") as sent:
            for _ in range(14):
                self.client.post(reverse("contacts"), {"text": "?", "name": "Аня", "contact": "@anya", "started": stamp(40)})

        self.assertEqual(sent.call_count, 10)
        self.assertEqual(Inquiry.objects.count(), 10)


class InquiryAdminTests(TestCase):
    """Отсеянное смотрят и возвращают в чат из админки."""

    def setUp(self):
        self.root = User.objects.create_superuser("root@t.local", "Анна", "Главная", password="pass12345")
        self.client.force_login(self.root)
        self.caught = Inquiry.objects.create(
            source=Inquiry.Source.CONTACTS, verdict=Inquiry.Verdict.SPAM, text="Пришлю диплом: https://disk.yandex.ru/i/abc",
            name="Markus", contact="markus@gmail.com", score=11, had_image=True,
            reasons=[["чужая ссылка", 6], ["ни одной русской буквы", 4], ["почта для ответа", 1]],
        )
        self.sent = Inquiry.objects.create(source=Inquiry.Source.SUPPORT, verdict=Inquiry.Verdict.SENT, text="Не работает")

    def release(self):
        with patch("core.inquiries.notify") as sent:
            self.client.post(reverse("admin:core_inquiry_changelist"), {
                "action": "release", "_selected_action": [self.caught.pk, self.sent.pk],
            })
        return sent

    def test_the_list_and_a_card_open(self):
        self.assertContains(self.client.get(reverse("admin:core_inquiry_changelist")), "Markus · markus@gmail.com")
        self.assertContains(self.client.get(reverse("admin:core_inquiry_change", args=[self.caught.pk])), "чужая ссылка +6")

    def test_a_caught_message_is_released_into_the_chat(self):
        sent = self.release()

        sent.assert_called_once()
        self.caught.refresh_from_db()
        self.assertEqual(self.caught.verdict, Inquiry.Verdict.RELEASED)
        text = render_to_string(*sent.call_args.args[1:])
        self.assertIn("Пришлю диплом", text)
        self.assertIn("переслано вручную", text)
        self.assertIn("картинка была, но не сохранилась", text)
        self.assertEqual(LogEntry.objects.get().change_message, "Не спам — переслано в чат")

    def test_what_was_not_caught_is_left_alone(self):
        self.release()

        self.sent.refresh_from_db()
        self.assertEqual(self.sent.verdict, Inquiry.Verdict.SENT)


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

    def test_only_noted_failures_count_and_forgiving_starts_over(self):
        """Пара для входа: проверка сама в счёт не идёт, иначе каждая удачная попытка
        приближала бы человека к запрету."""
        checks = []
        for _ in range(3):
            checks.append(exhausted("t:fails", 2))
            strike("t:fails")
        forgive("t:fails")

        self.assertEqual(checks, [False, False, True])
        self.assertFalse(exhausted("t:fails", 2))

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

        for name in ["about", "contacts", "support"]:
            with self.subTest(link=name):
                self.assertContains(page, f'href="{reverse(name)}"')
        self.assertContains(page, "vk.com/knt_mipt")

    def test_a_page_that_ends_keeps_its_footer(self):
        self.assertContains(self.client.get(reverse("bookmark_list")), "<footer")

    def test_a_guest_keeps_the_footer_he_has_nowhere_else_to_go_from(self):
        """Боковой панели у гостя нет, и ссылки ему больше взять неоткуда."""
        self.client.logout()

        self.assertContains(self.client.get(reverse("about")), "<footer")


class SkinTests(TestCase):
    """Событийный скин: класс `theme-<имя>` на <html> и блок правил в theme/input.css.

    Весь набор гоняется с закреплённым обычным видом (core/test_runner.py) — иначе
    в октябре он проверял бы одно, а в ноябре другое. Скин каждый тест включает сам.
    """

    CSS = settings.BASE_DIR / "theme" / "input.css"
    GHOST = 'x-data="ghost"'

    def setUp(self):
        self.client.force_login(make_user("skin@t.local"))

    def page(self, name="material_list"):
        return self.client.get(reverse(name)).content.decode()

    def test_halloween_lasts_all_of_october_and_not_a_day_more(self):
        for day, skin in [
            (date(2026, 9, 30), "default"), (date(2026, 10, 1), "halloween"),
            (date(2026, 10, 31), "halloween"), (date(2026, 11, 1), "default"),
        ]:
            with self.subTest(day=day):
                self.assertEqual(theme_for(day), skin)

    def test_the_test_run_itself_is_pinned_to_the_plain_look(self):
        self.assertEqual(settings.SITE_THEME, "default")
        self.assertIn('class="theme-default"', self.page())

    @override_settings(SITE_THEME=None)
    def test_left_to_itself_the_site_asks_the_calendar(self):
        with patch("core.context_processors.timezone.localdate", return_value=date(2026, 10, 15)):
            self.assertIn('class="theme-halloween"', self.page())
        with patch("core.context_processors.timezone.localdate", return_value=date(2026, 11, 15)):
            self.assertIn('class="theme-default"', self.page())

    @override_settings(SITE_THEME="halloween")
    def test_a_forced_skin_is_shown_whatever_the_date(self):
        """Так скин смотрят до его дат: ставят имя в настройках разработки."""
        with patch("core.context_processors.timezone.localdate", return_value=date(2026, 7, 1)):
            self.assertIn('class="theme-halloween"', self.page())

    def test_the_plain_logo_is_whitened_in_the_dark(self):
        page = self.page()

        self.assertIn("core/img/logo.svg", page)
        self.assertNotIn("skin-logo", page)
        self.assertIn("dark:invert", page)

    @override_settings(SITE_THEME="halloween")
    def test_the_halloween_logo_is_drawn_by_the_skin_and_stays_red_in_the_dark(self):
        """Логотип скина — маска в цвет акцента (.skin-logo), а не картинка. Обычный в тёмной
        теме белят фильтром; красный побелел бы тоже — и от скина в шапке не осталось бы ничего."""
        page = self.page()

        self.assertIn("skin-logo", page)
        self.assertNotIn("core/img/logo.svg", page)
        self.assertNotIn("dark:invert", page)

    @override_settings(SITE_THEME="halloween")
    def test_a_guest_gets_the_halloween_logo_too(self):
        self.client.logout()

        self.assertIn("skin-logo", self.page("login"))

    @override_settings(SITE_THEME="halloween")
    def test_the_ghost_sits_in_the_corner_of_an_ordinary_page(self):
        for name in ("material_list", "bookmark_list", "shop"):
            with self.subTest(page=name):
                self.assertIn(self.GHOST, self.page(name))

    @override_settings(SITE_THEME="halloween")
    def test_the_ghost_keeps_out_of_the_chat_and_the_wall(self):
        """В обоих разделах правый нижний угол окна занят: кнопка отправки и палитра."""
        Board.objects.create(title="Стена", width=8, height=4)

        for name in ("chat_list", "wall"):
            with self.subTest(page=name):
                page = self.page(name)

                self.assertIn('class="theme-halloween"', page)
                self.assertNotIn(self.GHOST, page)

    @override_settings(SITE_THEME="halloween")
    def test_a_guest_sees_no_ghost(self):
        self.client.logout()

        page = self.page("about")

        self.assertIn('class="theme-halloween"', page)
        self.assertNotIn(self.GHOST, page)

    def test_there_is_no_ghost_without_the_skin(self):
        self.assertNotIn(self.GHOST, self.page())

    def test_the_ornament_of_the_shell_is_for_those_who_are_in(self):
        """Паутина, луна с мышами и паук держатся за панель, шапку и колонку вошедшего.
        У гостя шапка другая, и крючков на ней нет: на витринных страницах орнамент свой."""
        inside = self.page()
        self.client.logout()
        outside = self.page("about")

        for hook in ("site-aside", "site-header", "site-body"):
            with self.subTest(hook=hook):
                self.assertIn(hook, inside)
                self.assertNotIn(hook, outside)

    def test_every_hook_the_skins_hang_on_is_there_in_the_markup(self):
        """Скин держится за классы-крючки. Переименуй такой в шаблоне — и оформление
        отвалится молча: ни ошибки, ни упавшей страницы."""
        hooks = set(re.findall(r"\.((?:skin|site)-[a-z-]+|page-title|brand-name)\b", self.CSS.read_text(encoding="utf-8")))
        markup = "\n".join(path.read_text(encoding="utf-8") for path in settings.BASE_DIR.glob("*/templates/**/*.html"))
        # Без комментариев: крючок, который остался только в пояснении к шаблону, — потерян.
        markup = re.sub(r"(?s){% comment %}.*?{% endcomment %}|{#.*?#}", "", markup)

        self.assertGreaterEqual(len(hooks), 12)
        for hook in sorted(hooks):
            with self.subTest(hook=hook):
                self.assertRegex(markup, rf"(?<![\w-]){re.escape(hook)}(?![\w-])")

    def test_every_file_the_stylesheet_points_to_is_in_place(self):
        """Ссылка в никуда роняет сборку статики на бою (StaticBuildTests), но тот тест
        видит только собранный css — а его в репозитории нет. Здесь смотрим в исходник."""
        static = settings.BASE_DIR / "core" / "static" / "core"
        urls = re.findall(r"url\('\.\./([^')]+)'\)", self.CSS.read_text(encoding="utf-8"))

        self.assertGreaterEqual(len(urls), 10)
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue((static / url).is_file())

    def test_every_picture_the_skin_markup_names_is_in_place(self):
        """На бою `{% static %}` на файл, которого нет, — это ошибка 500 на каждой странице."""
        for template in ("core/_logo.html", "core/halloween/_ghost.html"):
            source = (settings.BASE_DIR / "core" / "templates" / template).read_text(encoding="utf-8")
            names = re.findall(r"{% static '([^']+)' %}", source)

            self.assertTrue(names, template)
            for name in names:
                with self.subTest(file=name):
                    self.assertIsNotNone(finders.find(name))


class UnreadBadgeTests(SimpleTestCase):
    """Пустой счётчик непрочитанного не виден.

    Прячет его класс hidden, а раскладку даёт inline-flex — оба про display, и в собранном
    css побеждает тот, что ниже. Ниже оказался inline-flex, и у всех, кому никто не писал,
    рядом с «Сообщениями» стоял «0». Чинит это отдельное правило в theme/input.css; проверить
    его действие без браузера нельзя, поэтому сторожим хотя бы само правило.
    """

    def test_the_stylesheet_hides_the_empty_badge_by_a_rule_of_its_own(self):
        css = (settings.BASE_DIR / "theme" / "input.css").read_text(encoding="utf-8")

        self.assertRegex(css, r"(?m)^#unread-badge\.hidden \{\s*display: none;\s*\}")

    def test_the_badge_still_carries_both_classes_the_rule_is_there_for(self):
        """Уберут из разметки hidden или inline-flex — правило станет не нужно, и этот
        тест напомнит убрать и его."""
        badge = (settings.BASE_DIR / "chats" / "templates" / "chats" / "_unread_badge.html").read_text(encoding="utf-8")

        self.assertIn("inline-flex", badge)
        self.assertIn("{% if not unread_total %}hidden{% endif %}", badge)


def close_site(**fields):
    Lockdown(active=True, **fields).save()


def put_on_the_list(user):
    """В группу белого списка. Группу заводит миграция, но на тестовой базе, оставленной
    от прошлого прогона (--keepdb), её смывает очистка после TransactionTestCase."""
    group, _ = Group.objects.get_or_create(name=lockdown.GROUP)
    group.permissions.add(Permission.objects.get(content_type__app_label="core", codename="pass_lockdown"))
    user.groups.add(group)


class LockdownTests(TestCase):
    """Заслон: закрытый сайт отвечает одной страницей всем, кроме белого списка (core/lockdown.py)."""

    PAGE = "core/lockdown.html"
    LOGIN = {"username": "student@t.local", "password": "pass12345"}

    def setUp(self):
        self.student = make_user("student@t.local", name="Пётр", surname="Студентов")

    def assertShut(self, response):
        self.assertEqual(response.status_code, 503)
        self.assertTemplateUsed(response, self.PAGE)

    def assertInside(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertTemplateNotUsed(response, self.PAGE)

    # --- кого не пускает ---

    def test_an_open_site_does_not_notice_the_gate(self):
        self.assertInside(self.client.get(reverse("about")))

        # Запись есть, текст заготовлен, но галочка не стоит — это ещё не закрытие.
        Lockdown(active=False, text="черновик").save()

        self.assertInside(self.client.get(reverse("about")))

    def test_a_guest_gets_the_closed_page_at_every_address(self):
        """И на открытых всему интернету страницах, и там, где раньше ждала форма входа,
        и по адресу, которого нет вовсе."""
        close_site()

        for path in ("/", reverse("about"), reverse("contacts"), reverse("support"),
                     reverse("material_list"), reverse("admin:index"), "/no/such/page/"):
            with self.subTest(path=path):
                self.assertShut(self.client.get(path))

    def test_a_student_who_is_logged_in_gets_it_too(self):
        close_site()
        self.client.force_login(self.student)

        self.assertShut(self.client.get(reverse("material_list")))

    def test_a_moderator_is_not_let_in_for_being_one(self):
        close_site()
        self.client.force_login(make_user("mod@t.local", is_staff=True))

        self.assertShut(self.client.get(reverse("material_list")))

    def test_a_form_sent_to_a_closed_site_goes_nowhere(self):
        close_site()

        with patch("core.inquiries.notify") as notify:
            self.assertShut(self.client.post(reverse("contacts"), {"text": "Когда откроетесь?", "contact": "@me"}))
        notify.assert_not_called()

    def test_htmx_is_told_to_reload_the_page(self):
        """Кусок страницы закрытия посреди списка материалов был бы хуже, чем ничего."""
        close_site()

        response = self.client.get(reverse("material_list"), headers={"HX-Request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["HX-Refresh"], "true")
        self.assertEqual(response.content, b"")

    # --- кого пускает ---

    def test_the_group_of_the_white_list_lets_a_person_in(self):
        close_site()
        put_on_the_list(self.student)
        self.client.force_login(self.student)

        self.assertInside(self.client.get(reverse("material_list")))

    def test_the_right_given_to_a_person_directly_lets_in_too(self):
        close_site()
        self.student.user_permissions.add(Permission.objects.get(codename="pass_lockdown"))
        self.client.force_login(self.student)

        self.assertInside(self.client.get(reverse("material_list")))

    def test_a_superuser_needs_no_list(self):
        close_site()
        self.client.force_login(User.objects.create_superuser("boss@t.local", "Анна", "Главная", password="pass12345"))

        self.assertInside(self.client.get(reverse("material_list")))

    # --- дверь ---

    def test_the_way_in_stays_open(self):
        close_site()

        for path in (reverse("login"), reverse("password_reset"), reverse("password_reset_done"),
                     reverse("password_reset_confirm", args=["x", "y"]), reverse("password_reset_complete")):
            with self.subTest(path=path):
                self.assertInside(self.client.get(path))

    def test_the_first_password_can_still_be_changed(self):
        close_site()
        self.client.force_login(self.student)

        self.assertInside(self.client.get(reverse("password_change")))

    def test_every_open_name_is_a_real_address(self):
        """Переименуют урл — имя в списке перестанет совпадать, и раздача файлов или вход
        молча закроются вместе с сайтом."""
        known = get_resolver().reverse_dict

        for name in lockdown.OPEN:
            self.assertIn(name, known)

    def test_a_listed_person_logs_in_and_gets_the_site(self):
        close_site()
        put_on_the_list(self.student)

        self.assertInside(self.client.post(reverse("login"), self.LOGIN, follow=True))

    def test_an_unlisted_person_logs_in_stays_outside_and_can_leave(self):
        close_site()

        response = self.client.post(reverse("login"), self.LOGIN, follow=True)

        self.assertShut(response)
        page = response.content.decode()
        self.assertIn("Вы вошли как Студентов Пётр", page)
        self.assertIn(f'action="{reverse("logout")}"', page)

        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_guest_is_not_shown_the_door_for_the_team(self):
        """Ссылка на вход смущала бы всех, кому входить незачем; команда набирает адрес сама."""
        close_site()

        page = self.client.get("/").content.decode()

        self.assertNotIn(reverse("login"), page)
        self.assertNotIn(reverse("logout"), page)
        self.assertNotIn("<footer", page)

    # --- что идёт мимо ---

    def test_files_go_past_the_gate_and_do_not_ask_the_database(self):
        """Подпись кривая, поэтому 404 — но это ответ самой раздачи, а не заслона."""
        close_site()

        for path in (reverse("file_download", args=["x", "book.pdf"]), reverse("media_image", args=["x"]),
                     reverse("hls_piece", args=["x", "index.m3u8"])):
            with self.subTest(path=path), self.assertNumQueries(0):
                self.assertEqual(self.client.get(path).status_code, 404)

    @override_settings(INTAKE_TOKEN="token")
    def test_the_bakery_is_not_turned_away(self):
        close_site()

        response = self.client.get(reverse("intake_spec"), headers={"Authorization": "Bearer token"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        for pattern in intake_urls.urlpatterns:
            self.assertTrue(lockdown.always_open(reverse(pattern.name)), pattern.name)

    @override_settings(PRINT_AGENT_TOKEN="token")
    def test_the_print_agent_is_not_turned_away_but_the_print_pages_are_closed(self):
        close_site()

        response = self.client.get(reverse("printagent_config"), headers={"Authorization": "Bearer token"})

        self.assertEqual(response.status_code, 200)
        agent = [pattern.name for pattern in printing_urls.urlpatterns if pattern.name.startswith("printagent_")]
        self.assertGreater(len(agent), 5)
        for name in agent:
            self.assertTrue(lockdown.always_open(reverse(name)), name)
        self.assertFalse(lockdown.always_open(reverse("print_home")))

    @override_settings(ANALYTICS_TOKEN="token")
    def test_the_analytics_server_is_not_turned_away(self):
        close_site()

        response = self.client.post(reverse("analytics_inbox"), headers={"Authorization": "Bearer token"})

        # Без файла — отказ самой ручки, а не заслона: тот отвечал бы страницей и 503.
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"], "application/json")

    # --- сама страница ---

    def test_the_answer_says_it_is_temporary_and_must_not_be_kept(self):
        close_site()

        response = self.client.get(reverse("about"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "3600")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn('<meta name="robots" content="noindex">', response.content.decode())

    def test_the_page_carries_the_text(self):
        close_site(text="Хостинг **не оплачен**.<script>alert(1)</script>")

        page = self.client.get("/").content.decode()

        self.assertIn("Хостинг <strong>не оплачен</strong>.", page)
        self.assertNotIn("alert(1)", page)
        self.assertNotIn("Подробности появятся", page)

    def test_an_empty_record_leaves_a_stock_phrase_and_nothing_else(self):
        close_site()

        page = self.client.get("/").content.decode()

        self.assertIn("Подробности появятся", page)
        self.assertNotIn("Крайний срок", page)
        self.assertNotIn("По всем вопросам", page)
        self.assertIsNone(Lockdown.load().days_left)

    def test_the_deadline_is_written_out_and_counts_the_days_down(self):
        close_site(deadline=date(2026, 10, 29))

        self.assertIn(">29 октября 2026 года<", self.client.get("/").content.decode())

        today, after = timezone.localdate(), ". После этого срока сайт перестанет открываться."
        for days, words in ((21, "Остался 21 день" + after), (5, "Осталось 5 дней" + after),
                            (2, "Осталось 2 дня" + after), (1, "Остался 1 день" + after),
                            (0, "Сегодня последний день" + after), (-1, "Срок истёк")):
            with self.subTest(days=days):
                close_site(deadline=today + timedelta(days=days))

                self.assertIn(f">{words}<", self.client.get("/").content.decode())

    def test_the_deadline_can_say_what_is_due(self):
        close_site(deadline=date(2026, 10, 29))

        self.assertIn(">Крайний срок оплаты<", self.client.get("/").content.decode())

        close_site(deadline=date(2026, 10, 29), due=Lockdown.Due.STORAGE)

        self.assertIn(">Крайний срок оплаты · файловое хранилище<", self.client.get("/").content.decode())

    def test_the_contact_line_makes_a_link_of_the_address_only(self):
        close_site(contact="Иванов <b>Иван</b>, ivanov@phystech.edu")

        page = self.client.get("/").content.decode()

        self.assertIn("По всем вопросам: Иванов &lt;b&gt;Иван&lt;/b&gt;, ", page)
        self.assertIn('<a href="mailto:ivanov@phystech.edu">ivanov@phystech.edu</a>', page)

    @override_settings(SITE_THEME="halloween")
    def test_the_page_wears_no_skin_and_shows_no_menu(self):
        close_site()

        page = self.client.get(reverse("material_list")).content.decode()

        self.assertNotIn("theme-", page)
        self.assertNotIn("skin-", page)
        for name in ("material_list", "contacts", "support"):
            self.assertNotIn(f'href="{reverse(name)}"', page)

    # --- для белого списка ---

    def test_a_listed_person_is_reminded_that_the_site_is_closed(self):
        put_on_the_list(self.student)
        self.client.force_login(self.student)
        reminder = f'href="{reverse("lockdown")}"'

        self.assertNotIn(reminder, self.client.get(reverse("material_list")).content.decode())

        close_site()

        self.assertIn(reminder, self.client.get(reverse("material_list")).content.decode())
        # И по ссылке — страница, какой её видят остальные, но без отказа в ответе.
        response = self.client.get(reverse("lockdown"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, self.PAGE)
        self.assertContains(response, f'href="{reverse("home")}"')
        self.assertNotContains(response, f'action="{reverse("logout")}"')

    def test_the_draft_of_the_page_is_shown_to_its_editors_only(self):
        """Сайт открыт, а объявление заготовлено: студенту, забредшему на адрес, читать его незачем."""
        Lockdown(text="Черновик объявления").save()
        target = reverse("lockdown")

        self.assertRedirects(self.client.get(target), f"{reverse('login')}?next={target}")

        self.client.force_login(self.student)
        self.assertEqual(self.client.get(target).status_code, 404)

        # Белый список — про закрытый сайт; черновик он не открывает.
        put_on_the_list(self.student)
        self.assertEqual(self.client.get(target).status_code, 404)

        self.client.force_login(make_user("mod@t.local", is_staff=True))
        self.assertEqual(self.client.get(target).status_code, 404)

        editor = make_user("editor@t.local")
        editor.user_permissions.add(Permission.objects.get(codename="change_lockdown"))
        self.client.force_login(editor)
        response = self.client.get(target)
        self.assertContains(response, "Черновик объявления")
        self.assertContains(response, f'href="{reverse("home")}"')
        self.assertNotContains(response, f'action="{reverse("logout")}"')

        self.client.force_login(User.objects.create_superuser("boss@t.local", "Анна", "Главная", password="pass12345"))
        self.assertContains(self.client.get(target), "Черновик объявления")

    # --- выключатель ---

    def test_there_is_one_record_however_many_times_it_is_saved(self):
        self.assertFalse(Lockdown.load().active)
        self.assertEqual(Lockdown.objects.count(), 0)

        Lockdown(active=True).save()
        Lockdown(active=False, text="второй раз").save()

        self.assertEqual(Lockdown.objects.count(), 1)
        self.assertEqual(Lockdown.load().text, "второй раз")
        self.assertFalse(Lockdown.load().active)

    def test_the_admin_has_one_page_for_it_and_shows_who_gets_in(self):
        put_on_the_list(self.student)
        self.client.force_login(User.objects.create_superuser("boss@t.local", "Анна", "Главная", password="pass12345"))
        change = reverse("admin:core_lockdown_change", args=[1])

        self.assertRedirects(self.client.get(reverse("admin:core_lockdown_changelist")), change)
        page = self.client.get(change).content.decode()
        self.assertIn("Главная Анна", page)
        self.assertIn("Студентов Пётр", page)
        self.assertIn(lockdown.GROUP, page)

        self.assertEqual(self.client.get(reverse("admin:core_lockdown_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:core_lockdown_delete", args=[1])).status_code, 403)

    def test_the_tick_in_the_admin_closes_the_site(self):
        self.client.force_login(User.objects.create_superuser("boss@t.local", "Анна", "Главная", password="pass12345"))
        change = reverse("admin:core_lockdown_change", args=[1])
        self.client.get(reverse("admin:core_lockdown_changelist"))

        self.client.post(change, {"active": "on", "text": "Закрыто.", "due": "hosting", "deadline": "2026-10-29",
                                  "contact": "Иванов Иван, ivanov@phystech.edu"})

        self.assertShut(Client().get("/"))
        lock = Lockdown.load()
        self.assertEqual((lock.due, lock.deadline, lock.contact),
                         ("hosting", date(2026, 10, 29), "Иванов Иван, ivanov@phystech.edu"))

        self.client.post(change, {"text": "Закрыто.", "due": "", "deadline": "", "contact": ""})

        self.assertInside(Client().get(reverse("about")))

    def test_a_moderator_without_the_right_cannot_reach_the_switch(self):
        self.client.force_login(make_user("mod@t.local", is_staff=True))

        self.assertEqual(self.client.get(reverse("admin:core_lockdown_changelist")).status_code, 403)
        self.assertEqual(Lockdown.objects.count(), 0)

    def test_the_command_is_a_spare_switch(self):
        """Админка сама стоит за заслоном — на случай, если в неё не попасть."""
        put_on_the_list(self.student)
        out = StringIO()

        call_command("lockdown", "--on", stdout=out)
        call_command("lockdown", stdout=out)  # без ключей только рассказывает

        self.assertShut(self.client.get("/"))
        self.assertIn("Сайт закрыт", out.getvalue())
        self.assertIn("Студентов Пётр <student@t.local>", out.getvalue())

        call_command("lockdown", "--off", stdout=out)

        self.assertInside(self.client.get(reverse("about")))

    def test_the_migration_makes_the_group_with_the_one_right(self):
        migration = importlib.import_module("core.migrations.0002_lockdown")
        Group.objects.filter(name=lockdown.GROUP).delete()

        migration.add_group(django_apps, None)
        migration.add_group(django_apps, None)  # второй раз ничего не двоит

        group = Group.objects.get(name=migration.GROUP)
        rights = [f"{right.content_type.app_label}.{right.codename}" for right in group.permissions.all()]
        self.assertEqual(rights, [lockdown.PASS])
        self.assertEqual(migration.GROUP, lockdown.GROUP)

    def test_the_gate_stands_between_the_login_and_the_sockets(self):
        """Снаружи AuthMiddleware человек ещё не узнан, а мимо роутера — нечего закрывать."""
        from knt.asgi import application

        node = application.application_mapping["websocket"].application
        chain = []
        while hasattr(node, "inner"):
            chain.append(type(node).__name__)
            node = node.inner

        self.assertEqual(chain[-2:], ["AuthMiddleware", "SocketGate"])
        self.assertEqual(type(node).__name__, "URLRouter")


class LockdownSocketTests(TransactionTestCase):
    """Заслон на сокетах. TransactionTestCase — по той же причине, что и у сокета чата:
    в базу ходят из другого потока, и незакоммиченного теста он бы не увидел."""

    def setUp(self):
        self.student = make_user("student@t.local")

    async def knock(self, user):
        socket = WebsocketCommunicator(lockdown.SocketGate(ChatConsumer.as_asgi()), "/ws/chats/")
        # Человека читаем заново, как AuthMiddlewareStack на каждое соединение: права
        # кешируются на объекте, а у созданного в тесте кеш уже прогрет сигналом чатов.
        socket.scope["user"] = await database_sync_to_async(User.objects.get)(pk=user.pk)
        connected, _ = await socket.connect()
        await socket.disconnect()
        return connected

    async def test_an_open_site_lets_the_socket_through(self):
        self.assertTrue(await self.knock(self.student))

    async def test_a_closed_site_refuses_it(self):
        await database_sync_to_async(close_site)()

        self.assertFalse(await self.knock(self.student))

    async def test_but_not_to_a_listed_person(self):
        await database_sync_to_async(close_site)()
        await database_sync_to_async(put_on_the_list)(self.student)

        self.assertTrue(await self.knock(self.student))


class FailureJournalTests(TestCase):
    """Журнал сбоев (core/failures.py): что в него попадает и чем он никогда не мешает."""

    def test_the_same_failure_within_an_hour_is_counted_not_repeated(self):
        record(Failure.Source.PRINTING, "замятие")
        record(Failure.Source.PRINTING, "замятие")
        record(Failure.Source.PRINTING, "нет бумаги")

        self.assertEqual(
            sorted(Failure.objects.values_list("text", "times")), [("замятие", 2), ("нет бумаги", 1)],
        )

    def test_an_old_failure_coming_back_is_a_new_row(self):
        record(Failure.Source.PRINTING, "замятие")
        Failure.objects.update(last_at=timezone.now() - timedelta(hours=2))

        record(Failure.Source.PRINTING, "замятие")

        self.assertEqual(Failure.objects.count(), 2)

    def test_a_broken_record_does_not_break_the_caller_or_its_transaction(self):
        """Нулевой байт Postgres не примет. Без точки сохранения упавшая запись отравила
        бы транзакцию вызывающего, и следующий же его запрос упал бы."""
        with self.assertLogs("core.failures", "ERROR"):
            record(Failure.Source.TASK, "a\x00b")

        self.assertEqual(Failure.objects.count(), 0)

    def test_a_task_that_gave_up_lands_in_the_journal(self):
        """Бэкап падал молча — сбой оставался только в логе воркера."""
        # throw=False — как в воркере: с проброшенным исключением Celery сигнал о провале не шлёт.
        with patch("core.backup.make", side_effect=RuntimeError("pg_dump: нет связи с базой")):
            backup_database.apply(throw=False)

        failure = Failure.objects.get()
        self.assertEqual(failure.source, Failure.Source.TASK)
        self.assertIn("core.tasks.backup_database", failure.text)
        self.assertIn("pg_dump: нет связи с базой", failure.text)


    def test_the_journal_is_in_the_admin_for_reading_only(self):
        boss = User.objects.create_superuser("boss@t.local", "Анна", "Главная", password="pass12345")
        record(Failure.Source.MAIL, "кому a@t.local: отказ во входе")
        self.client.force_login(boss)

        self.assertContains(self.client.get(reverse("admin:core_failure_changelist")), "кому a@t.local: отказ во входе")
        self.assertEqual(self.client.get(reverse("admin:core_failure_add")).status_code, 403)


class HealthzTests(TestCase):
    """Ручка здоровья: по ней деплой узнаёт, что поднялась именно новая версия (tools/wait_healthy.sh)."""

    @override_settings(SITE_VERSION="0b26bbb · 2026-11-07 · Deploy epta")
    def test_a_guest_gets_the_running_commit_fresh(self):
        response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "ok 0b26bbb\n")
        # Закешированный ответ старого контейнера выдал бы мёртвый сайт за живой.
        self.assertIn("no-store", response["Cache-Control"])

    @override_settings(SITE_VERSION="")
    def test_without_a_version_it_is_just_ok(self):
        self.assertEqual(self.client.get(reverse("healthz")).content.decode(), "ok\n")

    def test_it_answers_while_the_site_is_closed(self):
        """Иначе выкладка во время закрытия считалась бы упавшей."""
        close_site()

        self.assertEqual(self.client.get(reverse("healthz")).status_code, 200)

    def test_a_dead_database_is_a_503(self):
        with patch("core.views.connection") as connection:
            connection.cursor.side_effect = OperationalError("нет связи")
            response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 503)


def _crash(request, pk):
    raise KeyError("files")


def _forbidden(request):
    raise PermissionDenied


class CrashUrls:
    """Настоящие адреса сайта и два своих: страницы ошибок ссылаются на главную и поддержку."""

    urlpatterns = [
        path("crash/<int:pk>/", login_not_required(_crash)),
        path("forbidden/", login_not_required(_forbidden)),
        *site_urls.urlpatterns,
    ]


@override_settings(ROOT_URLCONF=CrashUrls)
class ErrorPageTests(TestCase):
    """Пятисотки — в журнал сбоев, страницы ошибок — по-русски и сами по себе."""

    def setUp(self):
        # Как на проде: исключение превращается в страницу 500, а не летит в тест.
        self.client = Client(raise_request_exception=False)

    def test_a_crash_lands_in_the_journal_once_per_route(self):
        """Шаблоном адреса, а не путём: одна ошибка на разных материалах — одна строка."""
        self.client.get("/crash/7/")
        self.client.get("/crash/8/")

        failure = Failure.objects.get()
        self.assertEqual(failure.source, Failure.Source.WEB)
        self.assertEqual(failure.text, "GET /crash/<int:pk>/: KeyError: 'files'")
        self.assertEqual(failure.times, 2)

    def test_the_500_page_speaks_russian_and_stands_alone(self):
        response = self.client.get("/crash/7/")

        self.assertContains(response, "Что-то сломалось", status_code=500)
        self.assertTemplateUsed(response, "core/error.html")
        # Страница беды не должна упасть второй раз на шапке, меню и счётчиках.
        self.assertTemplateNotUsed(response, "core/base.html")

    def test_a_broken_journal_still_gives_the_500_page(self):
        with patch("core.failures.record", side_effect=RuntimeError("журнал сломан")), \
                self.assertLogs("core.failures", "ERROR"):
            response = self.client.get("/crash/7/")

        self.assertContains(response, "Что-то сломалось", status_code=500)

    def test_a_missing_page_is_not_a_failure(self):
        """Сканеры перебирают /wp-admin/ и прочее — журнал от них засорился бы за день."""
        response = self.client.get("/wp-admin/setup.php")

        self.assertContains(response, "Такой страницы нет", status_code=404)
        self.assertTemplateNotUsed(response, "core/base.html")
        self.assertEqual(Failure.objects.count(), 0)

    def test_a_refusal_is_explained_and_not_a_failure(self):
        response = self.client.get("/forbidden/")

        self.assertContains(response, "Сюда нельзя", status_code=403)
        self.assertEqual(Failure.objects.count(), 0)

    def test_a_stale_form_says_what_to_do(self):
        """Обычный случай — форма, открытая до входа в другой вкладке: вход сменил ключ."""
        client = Client(enforce_csrf_checks=True)
        client.cookies["csrftoken"] = "a" * 32

        response = client.post(reverse("support"), {"csrfmiddlewaretoken": "b" * 32})

        self.assertContains(response, "Форма устарела", status_code=403)
        self.assertContains(response, "обновите страницу", status_code=403)


class JournalSweepTests(TestCase):
    """Ночная уборка журналов: уходит старше JOURNAL_DAYS, свежее остаётся."""

    def test_old_entries_go_and_fresh_ones_stay(self):
        old = timezone.now() - timedelta(days=JOURNAL_DAYS + 1)
        LoginEvent.objects.create(kind=LoginEvent.Kind.FAILED, created=old)
        fresh_login = LoginEvent.objects.create(kind=LoginEvent.Kind.FAILED)
        Failure.objects.create(source=Failure.Source.TASK, text="давнее", created=old, last_at=old)
        # Началось давно, но повторялось недавно — это ещё живой сбой.
        recurring = Failure.objects.create(source=Failure.Source.TASK, text="хроническое", created=old)

        sweep_journals()

        self.assertEqual(list(LoginEvent.objects.all()), [fresh_login])
        self.assertEqual(list(Failure.objects.all()), [recurring])

    def test_inquiries_live_a_year(self):
        """Дольше журналов: по ним подкручивают фильтр спама, и сезон поступления нужен целиком."""
        now = timezone.now()
        Inquiry.objects.create(source="contacts", verdict="spam", text="давнее", created=now - timedelta(days=INQUIRY_DAYS + 1))
        kept = Inquiry.objects.create(source="contacts", verdict="sent", text="прошлой весной",
                                      created=now - timedelta(days=JOURNAL_DAYS + 1))

        self.assertIn("обращений: 1", sweep_journals())

        self.assertEqual(list(Inquiry.objects.all()), [kept])

    def test_expired_sessions_go_with_their_device_rows(self):
        """Django сам просроченные сессии не удаляет, и приписки «моих устройств» копились
        за ними: к 7 ноя 2026 на проде просрочены были 500 из 745."""
        person = make_user("sessions@x.ru")
        now = timezone.now()
        old = Session.objects.create(session_key="old", session_data="", expire_date=now - timedelta(hours=1))
        live = Session.objects.create(session_key="live", session_data="", expire_date=now + timedelta(days=1))
        UserSession.objects.create(session=old, user=person)
        UserSession.objects.create(session=live, user=person)

        self.assertIn("сессий: 1", sweep_journals())

        self.assertEqual(list(Session.objects.values_list("session_key", flat=True)), ["live"])
        self.assertEqual(list(UserSession.objects.values_list("session_id", flat=True)), ["live"])


class RunMarkTests(TestCase):
    """Последний запуск задачи — в кэше, для «Системы» Пульса (core/runs.py)."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_finished_task_leaves_its_mark(self):
        ping.apply(args=["pong"])

        mark = runs.last("core.tasks.ping")
        self.assertEqual((mark["ok"], mark["result"]), (True, "pong"))

    def test_a_failed_task_is_marked_as_failed(self):
        with patch("core.backup.make", side_effect=RuntimeError("pg_dump упал")):
            backup_database.apply(throw=False)

        mark = runs.last("core.tasks.backup_database")
        self.assertFalse(mark["ok"])
        self.assertIn("pg_dump упал", mark["result"])

    def test_a_dead_cache_breaks_neither_the_task_nor_the_page(self):
        with patch("core.runs.cache") as dead, self.assertLogs("core.runs", "ERROR") as log:
            dead.set.side_effect = dead.get.side_effect = ConnectionError("Redis лёг")
            self.assertEqual(ping.apply(args=["pong"]).get(), "pong")
            self.assertIsNone(runs.last("core.tasks.ping"))

        self.assertIn("Не записан последний запуск", log.output[0])
