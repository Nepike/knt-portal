"""Печать: разбор выбора и себестоимость, недельный лимит, страницы, админка и
чистые функции агента (tools/print_agent.py)."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import F
from django.test import SimpleTestCase, TestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from attachments.media import image_url
from attachments.storage import content_type, drop_prefix, file_storage
from attachments.tests import fake_storage
from attachments.uploads import adopt_token, under, uploaded_file
from core.models import Failure
from tools import print_agent
from users.models import User

from . import alerts, cleanup, queue, services, tasks, views
from .models import Printer, PrintAccount, PrintJob
from .pricing import BadSelection, Costs, cost, pages_of, rubles, selection_of, sheets, sides

MSK = ZoneInfo("Europe/Moscow")
# Цифры из плана (docs/printing.md, «Себестоимость»): там же посчитаны ожидаемые суммы.
PLAN = Costs(paper_sheet=100, toner_price=150000, toner_yield=7200, drum_price=300000, drum_yield=100000)
Status = PrintJob.Status


def make_user(email="p@t.local", *rights):
    user = User.objects.create_user(
        email=email, name="Иван", surname="Петров", password="pass12345", must_change_password=False,
    )
    user.user_permissions.add(*Permission.objects.filter(content_type__app_label="printing", codename__in=rights))
    return User.objects.get(pk=user.pk)  # права кешируются на объекте — берём свежий


def make_printer(**extra):
    """Принтер на связи: агент только что прислал показания."""
    fields = {
        "name": "Kyocera ECOSYS M2535dn", "queue": "m4-kyocera", "address": "192.168.16.41",
        "state": Printer.State.READY, "seen_at": timezone.now(), **extra,
    }
    return Printer.objects.create(**fields)


def second_printer(**extra):
    return make_printer(name="HP LaserJet", queue="m4-hp", address="192.168.16.42", **extra)


def make_job(user, printer, status=Status.READY, pages=4, coverage=None, **extra):
    folder = uuid4().hex
    return PrintJob.objects.create(
        uuid=UUID(folder), user=user, printer=printer, status=status, name="Конспект.pdf",
        source_key=f"printing/{folder}/Конспект.pdf", pdf_key=f"printing/{folder}/print.pdf",
        pages=pages, coverage=coverage or [0.05] * pages, **extra,
    )


class SelectionTests(SimpleTestCase):
    def test_ranges_and_single_pages(self):
        self.assertEqual(pages_of("1-3,7", 10), [1, 2, 3, 7])

    def test_spaces_dashes_and_repeats_are_forgiven(self):
        self.assertEqual(pages_of(" 2 – 4 ; 9, 3", 10), [2, 3, 4, 9])
        self.assertEqual(pages_of("5-3", 10), [3, 4, 5])

    def test_a_page_outside_the_document_is_refused(self):
        for text in ("0", "11", "9-11", "1-999999999"):
            with self.subTest(text=text), self.assertRaisesRegex(BadSelection, "в документе 10 страниц"):
                pages_of(text, 10)

    def test_the_page_count_is_said_in_proper_russian(self):
        for total, said in ((1, "1 страница"), (3, "3 страницы"), (11, "11 страниц"), (22, "22 страницы")):
            with self.subTest(total=total), self.assertRaisesRegex(BadSelection, f"в документе {said}$"):
                pages_of(str(total + 1), total)

    def test_garbage_is_refused(self):
        for text in ("abc", "1-", "-3", "1--2", "1.5", "2-три"):
            with self.subTest(text=text), self.assertRaisesRegex(BadSelection, "Не понял"):
                pages_of(text, 10)

    def test_nothing_chosen_is_refused_and_not_read_as_everything(self):
        """Снятые на экране галочки не должны превращаться в печать всего документа."""
        for text in ("", " , "):
            with self.subTest(text=text), self.assertRaisesRegex(BadSelection, "ни одной"):
                pages_of(text, 10)

    def test_the_selection_is_written_back_compactly(self):
        self.assertEqual(selection_of([7, 1, 2, 3]), "1-3,7")
        self.assertEqual(selection_of([5]), "5")
        self.assertEqual(selection_of([1, 2]), "1-2")
        for numbers in ([1], [1, 3, 5], [2, 3, 4, 8, 9, 12]):
            self.assertEqual(pages_of(selection_of(numbers), 12), numbers)


class PricingTests(SimpleTestCase):
    def test_a_page_of_text_costs_what_the_plan_says(self):
        self.assertEqual(cost([0.05], 1, False, PLAN), 124)

    def test_a_dark_slide_costs_what_the_plan_says(self):
        self.assertEqual(cost([0.40], 1, False, PLAN), 270)

    def test_duplex_saves_a_sheet_per_pair_but_not_toner(self):
        self.assertEqual(cost([0.05, 0.05], 1, False, PLAN) - cost([0.05, 0.05], 1, True, PLAN), PLAN.paper_sheet)

    def test_each_copy_starts_on_a_new_sheet(self):
        """Пять страниц с двух сторон — три листа на копию, а не пять листов на две копии."""
        self.assertEqual(sheets(5, 2, duplex=True), 6)
        self.assertEqual(sheets(5, 2), 10)
        self.assertEqual(sheets(4, 1, duplex=True), 2)

    def test_copies_multiply_the_cost(self):
        self.assertEqual(cost([0.05], 2, False, PLAN), 248)

    def test_the_toner_factor_scales_only_the_toner(self):
        doubled = Costs(**{**PLAN.as_dict(), "toner_factor": 2.0})
        self.assertEqual(cost([0.05], 1, False, doubled), 145)  # 100 + 2 × 20,83 + 3

    def test_the_limit_counts_sides_not_sheets(self):
        self.assertEqual(sides(5, 2), 10)

    def test_rubles(self):
        self.assertEqual(rubles(124), "1,24 ₽")
        self.assertEqual(rubles(5), "0,05 ₽")
        self.assertEqual(rubles(12000), "120,00 ₽")

    def test_pricing_imports_without_django(self):
        """Тот же разбор выбора берёт агент на М4, а Django там нет."""
        code = "import printing.pricing, sys; print('django' in sys.modules)"
        env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=settings.BASE_DIR,
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


class LimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.printer = make_printer()
        cls.user = make_user("p@t.local")

    def counted(self, at, status=Status.DONE, sides=5):
        return make_job(self.user, self.printer, status=status, sides=sides, submitted_at=at)

    def test_the_week_starts_on_monday_at_midnight_in_moscow(self):
        """В понедельник в 00:30 по Москве в Гринвиче ещё воскресенье: неделя по UTC
        считала бы воскресный вечер этой неделей."""
        now = datetime(2026, 10, 25, 21, 30, tzinfo=UTC)  # понедельник 00:30 в Москве
        self.counted(datetime(2026, 10, 25, 23, 50, tzinfo=MSK), sides=7)
        self.counted(datetime(2026, 10, 26, 0, 10, tzinfo=MSK), sides=3)

        self.assertEqual(services.week_start(now), datetime(2026, 10, 26, tzinfo=MSK))
        self.assertEqual(services.used(self.user, now), 3)

    def test_only_what_is_queued_printing_or_printed_counts(self):
        now = timezone.now()
        for status in Status:
            self.counted(now, status=status)

        self.assertEqual(services.used(self.user), 15)

    def test_someone_elses_pages_do_not_count(self):
        make_job(make_user("o@t.local"), self.printer, status=Status.DONE, sides=9, submitted_at=timezone.now())

        self.assertEqual(services.used(self.user), 0)

    def test_the_allowance_is_shared_personal_or_none(self):
        self.assertEqual(services.allowance(self.user), settings.PRINT_WEEKLY_PAGES)
        PrintAccount.objects.create(user=self.user, weekly_limit=20)
        self.assertEqual(services.allowance(self.user), 20)
        self.assertIsNone(services.allowance(make_user("d@t.local", "unlimited")))

    def test_printing_takes_a_snapshot_of_the_choice_and_the_prices(self):
        job = make_job(self.user, self.printer, coverage=[0.05, 0.4, 0.05, 0.1])

        services.submit(job, "1-2, 4", 2, True, "194.50.24.117")

        job.refresh_from_db()
        self.assertEqual(job.status, Status.QUEUED)
        self.assertEqual(job.selection, "1-2,4")
        self.assertEqual((job.copies, job.duplex, job.sides), (2, True, 6))
        self.assertEqual(job.cost, cost([0.05, 0.4, 0.1], 2, True, self.printer.costs()))
        self.assertEqual(job.costs, self.printer.costs().as_dict())
        self.assertFalse(job.outside_m4)
        self.assertIsNotNone(job.submitted_at)

    def test_printing_from_outside_m4_is_flagged_and_still_goes(self):
        job = services.submit(make_job(self.user, self.printer), "1", 1, False, "10.0.0.7")

        self.assertTrue(job.outside_m4)
        self.assertEqual(job.status, Status.QUEUED)

    def test_over_the_limit_is_refused_with_what_is_left(self):
        PrintAccount.objects.create(user=self.user, weekly_limit=5)
        job = make_job(self.user, self.printer)

        with self.assertRaisesRegex(services.Refused, "выбрано 8, осталось 5"):
            services.submit(job, "1-4", 2, False)

        job.refresh_from_db()
        self.assertEqual(job.status, Status.READY)

    def test_what_was_printed_this_week_is_taken_off_the_allowance(self):
        PrintAccount.objects.create(user=self.user, weekly_limit=10)
        self.counted(timezone.now(), sides=8)

        with self.assertRaisesRegex(services.Refused, "осталось 2"):
            services.submit(make_job(self.user, self.printer), "1-3", 1, False)

    def test_no_limit_with_the_unlimited_right(self):
        dean = make_user("d@t.local", "unlimited")
        PrintAccount.objects.create(user=dean, weekly_limit=1)

        job = services.submit(make_job(dean, self.printer), "1-4", 3, False)

        self.assertEqual(job.status, Status.QUEUED)

    @skipUnlessDBFeature("has_select_for_update")
    def test_the_account_is_locked_before_the_limit_is_read(self):
        """Две вкладки, нажатые разом, обе прочли бы «осталось 10» и поставили по десять."""
        job = make_job(self.user, self.printer)

        with CaptureQueriesContext(connection) as queries:
            services.submit(job, "1", 1, False)

        sql = [query["sql"] for query in queries.captured_queries]
        lock = next(i for i, text in enumerate(sql) if "printing_printaccount" in text and "FOR UPDATE" in text)
        total = next(i for i, text in enumerate(sql) if "SUM(" in text and "printing_printjob" in text)
        self.assertLess(lock, total)

    def test_the_same_job_is_not_queued_twice(self):
        job = make_job(self.user, self.printer)
        services.submit(job, "1-4", 1, False)

        with self.assertRaisesRegex(services.Refused, "уже не ждёт"):
            services.submit(job, "1-4", 1, False)
        self.assertEqual(services.used(self.user), 4)

    def test_duplex_is_off_where_the_printer_cannot(self):
        self.printer.duplex = False
        self.printer.save()

        job = services.submit(make_job(self.user, self.printer), "1-4", 1, True)

        self.assertFalse(job.duplex)

    def test_one_page_is_printed_one_sided(self):
        one = services.submit(make_job(self.user, self.printer), "2", 1, True)
        two = services.submit(make_job(self.user, self.printer), "1-2", 1, True)

        self.assertEqual((one.duplex, two.duplex), (False, True))

    def stored(self, job):
        for name in ("print.pdf", "pages/0001.jpg"):
            file_storage().save(f"{job.folder}{name}", ContentFile(b"x"))

    def test_remove_takes_the_job_and_its_files(self):
        job = make_job(self.user, self.printer)
        self.stored(job)

        with self.captureOnCommitCallbacks(execute=True):
            services.remove(job)

        self.assertFalse(PrintJob.objects.filter(pk=job.pk).exists())
        self.assertFalse(file_storage().exists(f"{job.folder}print.pdf"))

    def test_remove_leaves_the_files_a_copy_still_uses(self):
        done = make_job(self.user, self.printer, status=Status.DONE, sides=4, submitted_at=timezone.now())
        self.stored(done)
        copy = services.again(done)

        with self.captureOnCommitCallbacks(execute=True):
            services.remove(copy)

        self.assertFalse(PrintJob.objects.filter(pk=copy.pk).exists())
        self.assertTrue(file_storage().exists(f"{done.folder}print.pdf"))

    def test_what_counts_in_the_limit_cannot_be_removed(self):
        for status in PrintJob.COUNTED:
            job = make_job(self.user, self.printer, status=status, sides=2, submitted_at=timezone.now())
            with self.subTest(status=status), self.assertRaisesRegex(services.Refused, "удалить его нельзя"):
                services.remove(job)
            self.assertTrue(PrintJob.objects.filter(pk=job.pk).exists())

    def test_copies_are_sane(self):
        for copies in ("0", "21", "три", None):
            with self.subTest(copies=copies), self.assertRaises(services.Refused):
                services.submit(make_job(self.user, self.printer), "1", copies, False)

    def test_printing_on_another_printer_takes_its_prices(self):
        other = second_printer(paper_sheet=300)
        job = make_job(self.user, self.printer)

        services.submit(job, "1", 1, False, printer=other.pk)

        job.refresh_from_db()
        self.assertEqual(job.printer, other)
        self.assertEqual(job.cost, cost([0.05], 1, False, other.costs()))

    def test_a_printer_the_agent_has_not_heard_from_lately_is_refused(self):
        silent = second_printer(seen_at=timezone.now() - timedelta(minutes=6))

        with self.assertRaisesRegex(services.Refused, "нет связи"):
            services.submit(make_job(self.user, self.printer), "1", 1, False, printer=silent.pk)

    def test_a_printer_that_is_not_there_is_refused(self):
        for printer in ("999999", "абв"):
            with self.subTest(printer=printer), self.assertRaisesRegex(services.Refused, "Выбери принтер"):
                services.submit(make_job(self.user, self.printer), "1", 1, False, printer=printer)

    def test_an_inactive_printer_takes_nothing(self):
        self.printer.active = False
        self.printer.save()

        with self.assertRaisesRegex(services.Refused, "не принимает"):
            services.submit(make_job(self.user, self.printer), "1", 1, False)

    def test_cancel_gives_the_pages_back(self):
        job = services.submit(make_job(self.user, self.printer), "1-4", 1, False)

        services.cancel(job)

        job.refresh_from_db()
        self.assertEqual(job.status, Status.CANCELED)
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(services.used(self.user), 0)

    def test_what_the_agent_took_cannot_be_canceled(self):
        job = make_job(self.user, self.printer, status=Status.PRINTING)

        with self.assertRaisesRegex(services.Refused, "уже печатается"):
            services.cancel(job)

    def test_again_is_a_new_job_and_the_printed_one_still_counts(self):
        done = self.counted(timezone.now(), sides=4)
        done.selection, done.copies, done.duplex = "2-3", 2, False
        done.save()

        copy = services.again(done)

        self.assertNotEqual(copy.pk, done.pk)
        self.assertEqual(copy.status, Status.READY)
        self.assertEqual((copy.source_key, copy.pdf_key, copy.pages), (done.source_key, done.pdf_key, done.pages))
        self.assertEqual((copy.selection, copy.copies, copy.duplex), ("2-3", 2, False))
        self.assertEqual(copy.folder, done.folder)
        self.assertEqual(services.used(self.user), 4)

    def test_again_keeps_the_large_previews(self):
        done = self.counted(timezone.now())
        done.large = True
        done.save()

        self.assertTrue(services.again(done).large)

    def test_a_banned_student_cannot_print_or_repeat(self):
        done = self.counted(timezone.now())
        ready = make_job(self.user, self.printer)
        PrintAccount.objects.create(user=self.user, banned=True)

        with self.assertRaisesRegex(services.Refused, "закрыта"):
            services.submit(ready, "1", 1, False)
        with self.assertRaisesRegex(services.Refused, "закрыта"):
            services.again(done)

    def test_again_needs_the_files(self):
        done = self.counted(timezone.now())
        done.files_dropped = True

        with self.assertRaisesRegex(services.Refused, "удалён"):
            services.again(done)


class PageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.printer = make_printer(place="М4, у входа")
        cls.user = make_user("p@t.local")

    def setUp(self):
        cache.clear()  # ограничитель загрузок считает в кэше
        self.client.force_login(self.user)

    def send(self, name="Конспект.pdf", body=b"%PDF-1.7 test"):
        return self.client.post(reverse("print_upload"), {"file": SimpleUploadedFile(name, body)})

    def stored(self, name="Конспект.pdf", folder="printing"):
        key = f"{folder}/{uuid4().hex}/{name}"
        file_storage().save(key, ContentFile(b"%PDF-1.7 test"))
        return key, adopt_token(key, name)

    def test_every_student_can_print(self):
        """Обкатка кончилась: никаких прав, раздел видит и открывает любой вошедший."""
        self.client.force_login(make_user("s@t.local"))

        self.assertEqual(self.client.get(reverse("print_home")).status_code, 200)
        self.assertContains(self.client.get(reverse("wallet")), reverse("print_home"))

    def ban(self, user=None):
        PrintAccount.objects.update_or_create(user=user or self.user, defaults={"banned": True})

    def test_a_banned_student_sees_why_instead_of_the_upload_form(self):
        self.ban()

        response = self.client.get(reverse("print_home"))

        self.assertContains(response, services.BANNED)
        self.assertNotContains(response, f'hx-post="{reverse("print_upload")}"')

    def test_a_banned_student_cannot_upload(self):
        self.ban()

        self.send()

        self.assertFalse(PrintJob.objects.exists())

    @override_settings(R2_BUCKET="knt-files")
    def test_a_banned_student_gets_no_upload_link(self):
        self.ban()

        with mock.patch("attachments.uploads.file_storage", fake_storage):
            answer = self.client.post(
                reverse("print_sign"), json.dumps({"name": "x.pdf", "size": 10}), content_type="application/json",
            )

        self.assertEqual(answer.status_code, 403)

    def test_a_banned_student_cannot_print_what_was_already_uploaded(self):
        job = make_job(self.user, self.printer)
        self.ban()

        page = self.client.get(reverse("print_job", args=[job.uuid]))
        self.client.post(reverse("print_submit", args=[job.uuid]), {"selection": "1", "copies": "1"})

        self.assertNotIn("pick", page.context)
        self.assertContains(page, services.BANNED)
        job.refresh_from_db()
        self.assertEqual(job.status, Status.READY)

    def test_someone_elses_ban_is_not_mine(self):
        self.ban(make_user("o@t.local"))

        self.assertNotContains(self.client.get(reverse("print_home")), services.BANNED)

    def test_the_page_shows_the_printer_and_the_week(self):
        Printer.objects.filter(pk=self.printer.pk).update(seen_at=None)  # агент ещё ни разу не отзывался
        make_job(self.user, self.printer, status=Status.DONE, sides=37, submitted_at=timezone.now())

        response = self.client.get(reverse("print_home"))

        self.assertContains(response, "М4, у входа")
        self.assertContains(response, "Нет связи с принтером")
        self.assertEqual(response.context["limits"], {"used": 37, "allowance": 100, "left": 63})

    def test_a_file_sent_through_the_site_becomes_a_job(self):
        response = self.send()

        job = PrintJob.objects.get()
        self.assertRedirects(response, reverse("print_job", args=[job.uuid]))
        self.assertEqual((job.status, job.name, job.user, job.printer), (Status.UPLOADED, "Конспект.pdf", self.user, self.printer))
        self.assertTrue(job.source_key.startswith(f"printing/{job.uuid.hex}/"))
        self.assertTrue(file_storage().exists(job.source_key))

    def test_what_the_agent_cannot_print_is_refused_at_the_door(self):
        response = self.send("таблица.xlsx", b"PK\x03\x04")

        self.assertRedirects(response, reverse("print_home"))
        self.assertFalse(PrintJob.objects.exists())

    @override_settings(PRINT_MAX_FILE_MB=0.0001)
    def test_a_file_too_heavy_is_refused(self):
        self.send(body=b"%PDF" + b"0" * 200)

        self.assertFalse(PrintJob.objects.exists())

    def test_a_direct_upload_becomes_a_job_in_its_own_folder(self):
        key, token = self.stored()

        response = self.client.post(reverse("print_upload"), {"uploaded": token}, headers={"Accept": "application/json"})

        job = PrintJob.objects.get()
        self.assertEqual(response.json(), {"redirect": reverse("print_job", args=[job.uuid])})
        self.assertEqual((job.source_key, job.name), (key, "Конспект.pdf"))
        self.assertEqual(job.uuid.hex, key.split("/")[1])

    def test_the_same_upload_sent_twice_is_one_job(self):
        _, token = self.stored()

        for _ in range(2):
            self.client.post(reverse("print_upload"), {"uploaded": token})

        self.assertEqual(PrintJob.objects.count(), 1)

    def test_a_token_from_another_folder_is_not_taken(self):
        """Загрузка материала — не файл для печати: папки убираются разными уборками."""
        _, token = self.stored(folder="uploads")

        self.client.post(reverse("print_upload"), {"uploaded": token})

        self.assertFalse(PrintJob.objects.exists())

    @override_settings(PRINT_MAX_FILE_MB=0.00001)
    def test_a_direct_upload_heavier_than_signed_is_refused_and_removed(self):
        """Размер в подпись не входит: по ссылке можно положить сколько угодно."""
        key, token = self.stored()

        self.client.post(reverse("print_upload"), {"uploaded": token})

        self.assertFalse(PrintJob.objects.exists())
        self.assertFalse(file_storage().exists(key))

    @override_settings(R2_BUCKET="knt-files")
    def test_signing_puts_the_file_into_the_printing_folder(self):
        with mock.patch("attachments.uploads.file_storage", fake_storage):
            ask = lambda name: self.client.post(  # noqa: E731
                reverse("print_sign"), json.dumps({"name": name, "size": 1024}), content_type="application/json",
            )
            good, bad = ask("Слайды.pptx"), ask("таблица.xlsx")

        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()["type"], content_type("x.pptx"))
        key, _ = uploaded_file(good.json()["token"], "printing")
        self.assertRegex(key, r"^printing/[0-9a-f]{32}/Слайды\.pptx$")
        self.assertEqual(bad.status_code, 400)

    def test_uploads_are_throttled(self):
        with mock.patch.object(views, "UPLOADS_PER_HOUR", 1):
            self.send()
            self.send()

        self.assertEqual(PrintJob.objects.count(), 1)

    def test_someone_elses_job_is_not_found(self):
        job = make_job(make_user("o@t.local"), self.printer)

        self.assertEqual(self.client.get(reverse("print_job", args=[job.uuid])).status_code, 404)

    def test_a_ready_job_shows_its_pages_and_marks_the_dark_ones(self):
        job = make_job(self.user, self.printer, pages=2, coverage=[0.05, 0.6], sideways=[1])

        response = self.client.get(reverse("print_job", args=[job.uuid]))

        self.assertContains(response, image_url(f"{job.folder}pages/0002.jpg"))
        self.assertContains(response, "тёмная · 60%")
        self.assertContains(response, "поперёк листа")
        self.assertEqual(response.context["pick"]["selection"], "1-2")
        self.assertContains(response, 'id="print-pick"')

    def test_the_list_shows_what_a_printed_job_cost(self):
        make_job(self.user, self.printer, status=Status.DONE, sides=9, cost=1827, submitted_at=timezone.now())

        self.assertContains(self.client.get(reverse("print_home")), "18,27 ₽")

    def test_an_unavailable_printer_is_shown_but_cannot_be_chosen(self):
        off = second_printer(active=False)
        job = make_job(self.user, self.printer)

        response = self.client.get(reverse("print_job", args=[job.uuid]))

        self.assertContains(response, "Не принимает задания")
        self.assertRegex(response.content.decode(), rf'value="{off.pk}"[^>]*\sdisabled>')
        self.assertEqual(set(response.context["pick"]["printers"]), {self.printer.pk})

    def test_when_the_jobs_printer_is_down_another_is_offered(self):
        other = second_printer()
        Printer.objects.filter(pk=self.printer.pk).update(seen_at=None)
        job = make_job(self.user, self.printer)

        response = self.client.get(reverse("print_job", args=[job.uuid]))

        self.assertEqual(response.context["pick"]["printer"], other.pk)

    def test_the_printer_is_chosen_on_the_page(self):
        other = second_printer()
        job = make_job(self.user, self.printer)

        self.client.post(reverse("print_submit", args=[job.uuid]), {"selection": "1", "copies": "1", "printer": other.pk})

        job.refresh_from_db()
        self.assertEqual((job.status, job.printer), (Status.QUEUED, other))

    def test_a_page_shows_a_spinner_until_its_picture_arrives(self):
        """Первые страницы видны сразу — их не откладываем; дальние грузятся, когда до них долистают."""
        job = make_job(self.user, self.printer, pages=12)

        response = self.client.get(reverse("print_job", args=[job.uuid]))

        self.assertContains(response, 'class="print-page-wait"', count=12)
        self.assertContains(response, 'loading="lazy"', count=2)

    def test_remove_from_the_page(self):
        job = make_job(self.user, self.printer)

        response = self.client.post(reverse("print_remove", args=[job.uuid]), follow=True)

        self.assertRedirects(response, reverse("print_home"))
        self.assertContains(response, "удалено")
        self.assertFalse(PrintJob.objects.filter(pk=job.pk).exists())

    def test_someone_elses_job_cannot_be_removed(self):
        job = make_job(make_user("o@t.local"), self.printer)

        self.assertEqual(self.client.post(reverse("print_remove", args=[job.uuid])).status_code, 404)
        self.assertTrue(PrintJob.objects.filter(pk=job.pk).exists())

    def test_the_remove_button_is_only_where_removing_is_allowed(self):
        ready = make_job(self.user, self.printer)
        done = make_job(self.user, self.printer, status=Status.DONE, sides=4, selection="1-4", submitted_at=timezone.now())

        self.assertContains(self.client.get(reverse("print_job", args=[ready.uuid])), reverse("print_remove", args=[ready.uuid]))
        self.assertNotContains(self.client.get(reverse("print_job", args=[done.uuid])), reverse("print_remove", args=[done.uuid]))

    def test_a_one_page_document_is_offered_as_page_1(self):
        job = make_job(self.user, self.printer, pages=1)

        self.assertEqual(self.client.get(reverse("print_job", args=[job.uuid])).context["pick"]["selection"], "1")

    def test_the_viewer_gets_large_pages_when_the_agent_drew_them(self):
        small = make_job(self.user, self.printer, pages=2)
        large = make_job(self.user, self.printer, pages=2, large=True)

        zoom = lambda job: self.client.get(reverse("print_job", args=[job.uuid])).context["zoom"]  # noqa: E731

        self.assertEqual(zoom(small)[1], {"url": image_url(f"{small.folder}pages/0002.jpg"), "name": "Страница 2"})
        self.assertEqual(zoom(large)[1]["url"], image_url(f"{large.folder}large/0002.jpg"))

    def test_a_printed_job_opens_its_pages_in_the_viewer(self):
        job = make_job(self.user, self.printer, status=Status.DONE, sides=4, selection="1-4", submitted_at=timezone.now())

        response = self.client.get(reverse("print_job", args=[job.uuid]))

        self.assertContains(response, 'id="print-zoom"')
        self.assertContains(response, '@click="show(0)"')

    def test_printing_from_the_page(self):
        job = make_job(self.user, self.printer)

        response = self.client.post(reverse("print_submit", args=[job.uuid]), {"selection": "1-2", "copies": "1", "duplex": "on"})

        self.assertRedirects(response, reverse("print_job", args=[job.uuid]))
        job.refresh_from_db()
        self.assertEqual((job.status, job.sides, job.duplex), (Status.QUEUED, 2, True))

    def test_a_refusal_is_said_on_the_page(self):
        PrintAccount.objects.create(user=self.user, weekly_limit=1)
        job = make_job(self.user, self.printer)

        response = self.client.post(reverse("print_submit", args=[job.uuid]), {"selection": "1-4", "copies": "1"}, follow=True)

        self.assertContains(response, "Не влезает в недельный лимит")

    def test_the_poll_redraws_the_page_only_when_the_status_changes(self):
        job = make_job(self.user, self.printer, status=Status.QUEUED, sides=4, selection="1-4")
        poll = lambda was: self.client.get(reverse("print_status", args=[job.uuid]), {"was": was})  # noqa: E731

        self.assertEqual(poll("analyzing")["HX-Refresh"], "true")
        same = poll("queued")
        self.assertNotIn("HX-Refresh", same)
        self.assertContains(same, "В очереди")

    def test_a_waiting_job_keeps_asking_and_a_finished_one_stops(self):
        job = make_job(self.user, self.printer, status=Status.QUEUED, sides=4, selection="1-4")
        self.assertContains(self.client.get(reverse("print_job", args=[job.uuid])), 'hx-trigger="every 2s"')

        job.status = Status.DONE
        job.save()
        self.assertNotContains(self.client.get(reverse("print_job", args=[job.uuid])), 'hx-trigger="every 2s"')

    def test_outside_m4_there_is_a_warning(self):
        job = make_job(self.user, self.printer)

        self.assertContains(self.client.get(reverse("print_job", args=[job.uuid])), "не в здании М4")
        inside = self.client.get(reverse("print_job", args=[job.uuid]), REMOTE_ADDR=settings.PRINT_M4_IPS[0])
        self.assertNotContains(inside, "не в здании М4")

    def test_cancel_and_print_again_from_the_page(self):
        job = services.submit(make_job(self.user, self.printer), "1", 1, False)

        self.client.post(reverse("print_cancel", args=[job.uuid]))
        response = self.client.post(reverse("print_again", args=[job.uuid]))

        copy = PrintJob.objects.exclude(pk=job.pk).get()
        self.assertRedirects(response, reverse("print_job", args=[copy.uuid]))
        self.assertEqual(copy.status, Status.READY)


class AdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.printer = make_printer()
        cls.user = make_user("p@t.local")
        cls.boss = User.objects.create_superuser("boss@t.local", "Анна", "Главная", password="pass12345")

    def setUp(self):
        self.client.force_login(self.boss)

    def test_the_pages_open(self):
        job = make_job(self.user, self.printer)
        PrintAccount.objects.create(user=self.user, weekly_limit=10)
        for name in ("printer", "printjob", "printaccount", "printerreading"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"admin:printing_{name}_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:printing_printjob_change", args=[job.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:printing_printer_change", args=[self.printer.pk])).status_code, 200)

    def test_marking_failed_touches_only_stuck_jobs(self):
        lost = make_job(self.user, self.printer, status=Status.UNKNOWN)
        printed = make_job(self.user, self.printer, status=Status.DONE)

        self.client.post(reverse("admin:printing_printjob_changelist"), {
            "action": "mark_failed", "_selected_action": [lost.pk, printed.pk],
        })

        lost.refresh_from_db()
        printed.refresh_from_db()
        self.assertEqual((lost.status, printed.status), (Status.FAILED, Status.DONE))
        self.assertIsNotNone(lost.finished_at)

    def test_the_week_filter(self):
        start = services.week_start()
        this = make_job(self.user, self.printer, status=Status.DONE, submitted_at=start + timedelta(hours=1))
        last = make_job(self.user, self.printer, status=Status.DONE, submitted_at=start - timedelta(days=2))

        shown = lambda week: list(self.client.get(  # noqa: E731
            reverse("admin:printing_printjob_changelist"), {"week": week},
        ).context["cl"].result_list)

        self.assertEqual(shown("0"), [this])
        self.assertEqual(shown("1"), [last])
        self.assertEqual(set(shown("4")), {this, last})


class AgentTests(SimpleTestCase):
    """Чистые функции агента. Сам разбор документа гоняется на М4 — там LibreOffice и gs."""

    def file(self, name, body):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / name
        path.write_bytes(body)
        return path

    def test_pages_are_read_one_after_another_past_header_comments(self):
        stream = io.BytesIO(b"P5\n# gs\n2 1\n255\n" + bytes([0, 255]) + b"P5 1 1 255\n\x80")

        self.assertEqual(list(print_agent.gray_pages(stream)), [b"\x00\xff", b"\x80"])

    def test_a_stream_broken_off_mid_page_is_noticed(self):
        with self.assertRaisesRegex(ValueError, "оборвался"):
            list(print_agent.gray_pages(io.BytesIO(b"P5 2 2 255\n\x00")))

    def test_something_other_than_gray_is_refused(self):
        with self.assertRaisesRegex(ValueError, "не PGM"):
            list(print_agent.gray_pages(io.BytesIO(b"P6 1 1 255\n\x00\x00\x00")))

    def test_darkness(self):
        self.assertEqual(print_agent.darkness(bytes([255] * 100)), 0)
        self.assertEqual(print_agent.darkness(bytes(100)), 1)
        self.assertEqual(print_agent.darkness(bytes([0, 255])), 0.5)

    def test_the_file_must_be_what_its_name_says(self):
        """LibreOffice прочтёт что угодно и напечатает страницу мусора."""
        with self.assertRaisesRegex(print_agent.Rejected, "повреждён"):
            print_agent.accept(self.file("Курсовая.docx", b"hello, world"))
        self.assertEqual(print_agent.accept(self.file("Курсовая.docx", b"PK\x03\x04rest")), ".docx")
        self.assertEqual(print_agent.accept(self.file("Слайды.ppt", print_agent.OLE + b"rest")), ".ppt")

    def test_a_pdf_may_start_with_a_little_junk(self):
        self.assertEqual(print_agent.accept(self.file("a.pdf", b"\r\n" * 50 + b"%PDF-1.4")), ".pdf")
        with self.assertRaises(print_agent.Rejected):
            print_agent.accept(self.file("b.pdf", b" " * 2000 + b"%PDF-1.4"))

    def test_tables_and_empty_files_are_refused(self):
        with self.assertRaisesRegex(print_agent.Rejected, "Таблицы"):
            print_agent.accept(self.file("Ведомость.xlsx", b"PK\x03\x04"))
        with self.assertRaisesRegex(print_agent.Rejected, "пустой"):
            print_agent.accept(self.file("пусто.pdf", b""))

    def test_the_site_takes_exactly_what_the_agent_can_print(self):
        self.assertEqual(set(views.ACCEPTED), print_agent.ACCEPTED)

    def test_every_accepted_file_is_stored_with_its_own_type(self):
        for suffix in views.ACCEPTED:
            with self.subTest(suffix=suffix):
                self.assertNotEqual(content_type(f"x{suffix}"), "application/octet-stream")


TOKEN = "agent-token-123"


@override_settings(PRINT_AGENT_TOKEN=TOKEN)
class AgentCase(TestCase):
    """Ручки агента. Подписи R2 подменены: в тестах бакета нет, а договор — в ключах."""

    @classmethod
    def setUpTestData(cls):
        cls.printer = make_printer()
        cls.user = make_user("p@t.local")

    def setUp(self):
        for name, fake in (("sign_download", lambda key: "https://r2/get"), ("sign_put", lambda key: f"https://r2/{key}")):
            patch = mock.patch(f"printing.agent.{name}", side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, door, payload=None, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if door == "config":
            return self.client.get(reverse("printagent_config"), headers=headers)
        return self.client.post(
            reverse(f"printagent_{door}"), json.dumps(payload or {}), content_type="application/json", headers=headers,
        )

    def claim(self, kind):
        return self.call("claim", {"kind": kind}).json()["job"]

    def uploaded(self, **extra):
        return make_job(self.user, self.printer, status=Status.UPLOADED, pages=0, **extra)

    def queued(self, printer=None, **extra):
        fields = {"selection": "1-2", "copies": 2, "duplex": True, "sides": 4, "submitted_at": timezone.now(), **extra}
        return make_job(self.user, printer or self.printer, status=Status.QUEUED, **fields)

    def quiet(self, job, minutes):
        PrintJob.objects.filter(pk=job.pk).update(heard_at=timezone.now() - timedelta(minutes=minutes))


class AgentDoorTests(AgentCase):
    def test_a_stranger_is_not_let_in(self):
        self.assertEqual(self.call("config", token=None).status_code, 401)
        self.assertEqual(self.call("config", token="ne-tot").status_code, 401)

    @override_settings(INTAKE_TOKEN="bakery-token")
    def test_the_bakerys_token_does_not_open_printing(self):
        """Токены разные: пекарню и печать отзывают независимо."""
        self.assertEqual(self.call("config", token="bakery-token").status_code, 401)

    @override_settings(PRINT_AGENT_TOKEN="")
    def test_an_empty_token_closes_the_doors(self):
        answer = self.call("claim", {"kind": "print"})

        self.assertEqual(answer.status_code, 503)
        self.assertIn("PRINT_AGENT_TOKEN", answer.json()["error"])

    def test_config_tells_printers_limits_and_silences(self):
        answer = self.call("config").json()

        self.assertEqual(answer["printers"][0]["queue"], "m4-kyocera")
        self.assertEqual(answer["limits"]["max_doc_pages"], settings.PRINT_MAX_DOC_PAGES)
        self.assertEqual(answer["silence"], {"analyze": 300, "print": 120, "sent": 1800})

    def test_an_unknown_kind_of_work_is_refused(self):
        self.assertEqual(self.call("claim", {"kind": "scan"}).status_code, 400)

    def test_a_body_that_is_not_an_object_is_refused_not_a_crash(self):
        answer = self.client.post(
            reverse("printagent_claim"), "[1, 2]", content_type="application/json",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        self.assertEqual(answer.status_code, 400)


class AnalyzeQueueTests(AgentCase):
    def test_the_oldest_upload_goes_first_and_only_once(self):
        older = self.uploaded(created=timezone.now() - timedelta(minutes=3))
        newer = self.uploaded()

        first, second, third = self.claim("analyze"), self.claim("analyze"), self.claim("analyze")

        self.assertEqual((first["uuid"], second["uuid"], third), (older.uuid.hex, newer.uuid.hex, None))
        older.refresh_from_db()
        self.assertEqual((older.status, older.attempts), (Status.ANALYZING, 1))

    def test_the_job_brings_what_the_agent_needs(self):
        job = self.uploaded()

        given = self.claim("analyze")

        self.assertEqual((given["extension"], given["source"], given["queue"]), (".pdf", "https://r2/get", "m4-kyocera"))
        self.assertEqual(given["name"], job.name)

    def test_a_silent_analysis_goes_out_again_and_the_old_token_dies(self):
        job = self.uploaded()
        old = self.claim("analyze")["token"]
        self.quiet(job, 6)

        again = self.claim("analyze")

        self.assertEqual(again["uuid"], job.uuid.hex)
        self.assertEqual(self.call("analyzed", {"token": old, "pages": 1, "coverage": [0.1]}).status_code, 409)

    def test_a_working_analysis_is_not_taken_away(self):
        job = self.uploaded()
        self.claim("analyze")
        self.quiet(job, 4)

        self.assertIsNone(self.claim("analyze"))

    def test_a_file_the_agent_keeps_failing_on_is_rejected(self):
        job = self.uploaded(tries=queue.ANALYZE_TRIES)

        self.assertIsNone(self.claim("analyze"))
        job.refresh_from_db()
        self.assertEqual(job.status, Status.REJECTED)
        self.assertIn("в PDF", job.reason)

    def test_the_numbers_are_checked(self):
        self.uploaded()
        token = self.claim("analyze")["token"]
        too_many = settings.PRINT_MAX_DOC_PAGES + 1
        for wrong in (
            {"pages": 0, "coverage": []},
            {"pages": too_many, "coverage": [0.1] * too_many},
            {"pages": "2", "coverage": [0.1, 0.1]},
            {"pages": 2, "coverage": [0.1]},
            {"pages": 2, "coverage": [0.1, 1.5]},
            {"pages": 2, "coverage": [0.1, "много"]},
            {"pages": 2, "coverage": [0.1, 0.1], "sideways": [3]},
        ):
            with self.subTest(wrong=wrong):
                self.assertEqual(self.call("analyzed", {"token": token, **wrong}).status_code, 400)

    def test_the_site_chooses_where_the_agent_puts_the_pages(self):
        job = self.uploaded()
        token = self.claim("analyze")["token"]

        answer = self.call("analyzed", {"token": token, "pages": 2, "coverage": [0.05, 0.3], "sideways": [2]}).json()

        self.assertEqual(answer["urls"], {
            "print.pdf": f"https://r2/{job.folder}print.pdf",
            "pages/0001.jpg": f"https://r2/{job.folder}pages/0001.jpg",
            "pages/0002.jpg": f"https://r2/{job.folder}pages/0002.jpg",
        })
        self.assertEqual(answer["large"], {
            "large/0001.jpg": f"https://r2/{job.folder}large/0001.jpg",
            "large/0002.jpg": f"https://r2/{job.folder}large/0002.jpg",
        })
        self.assertEqual(answer["types"]["pages/0001.jpg"], "image/jpeg")
        self.assertEqual(answer["types"]["large/0002.jpg"], "image/jpeg")
        self.assertEqual(answer["types"]["print.pdf"], "application/pdf")
        job.refresh_from_db()
        self.assertEqual((job.pages, job.coverage, job.sideways), (2, [0.05, 0.3], [2]))

    def test_commit_checks_that_everything_is_there(self):
        job = self.uploaded()
        token = self.claim("analyze")["token"]
        self.call("analyzed", {"token": token, "pages": 2, "coverage": [0.05, 0.3]})
        half = {f"{job.folder}print.pdf", f"{job.folder}pages/0001.jpg"}

        with mock.patch("printing.agent.under", return_value=half):
            short = self.call("commit", {"token": token})
        with mock.patch("printing.agent.under", return_value=half | {f"{job.folder}pages/0002.jpg"}):
            whole = self.call("commit", {"token": token})

        self.assertEqual(short.status_code, 400)
        self.assertIn("нет 1 из 3", short.json()["error"])
        self.assertEqual(whole.status_code, 200)
        job.refresh_from_db()
        self.assertEqual((job.status, job.pdf_key), (Status.READY, f"{job.folder}print.pdf"))
        self.assertIsNotNone(job.analyzed_at)

    def test_commit_notes_whether_the_large_previews_came(self):
        """Крупных может не быть (агент старше) — печатать можно, просмотрщик возьмёт мелкие."""
        job = self.uploaded()
        token = self.claim("analyze")["token"]
        self.call("analyzed", {"token": token, "pages": 2, "coverage": [0.05, 0.3]})
        small = {f"{job.folder}print.pdf", f"{job.folder}pages/0001.jpg", f"{job.folder}pages/0002.jpg"}

        with mock.patch("printing.agent.under", return_value=small | {f"{job.folder}large/0001.jpg"}):
            self.call("commit", {"token": token})
        job.refresh_from_db()
        self.assertEqual((job.status, job.large), (Status.READY, False))

        PrintJob.objects.filter(pk=job.pk).update(status=Status.ANALYZING)
        with mock.patch("printing.agent.under", return_value=small | {f"{job.folder}large/0001.jpg", f"{job.folder}large/0002.jpg"}):
            self.call("commit", {"token": token})
        job.refresh_from_db()
        self.assertTrue(job.large)

    def test_commit_before_the_numbers_is_refused(self):
        self.uploaded()
        token = self.claim("analyze")["token"]

        self.assertEqual(self.call("commit", {"token": token}).status_code, 400)

    def test_a_refusal_reaches_the_student(self):
        job = self.uploaded()
        token = self.claim("analyze")["token"]

        self.call("fail", {"token": token, "error": "PDF защищён паролем. Снимите защиту и загрузите снова."})

        job.refresh_from_db()
        self.assertEqual((job.status, job.reason), (Status.REJECTED, "PDF защищён паролем. Снимите защиту и загрузите снова."))
        # Беда файла, а не системы: в журнал сбоев такое не идёт.
        self.assertFalse(Failure.objects.exists())

    def test_release_puts_the_file_back_in_line(self):
        job = self.uploaded()
        token = self.claim("analyze")["token"]

        self.call("release", {"token": token})

        job.refresh_from_db()
        self.assertEqual(job.status, Status.UPLOADED)
        self.assertEqual(self.claim("analyze")["uuid"], job.uuid.hex)

    def test_alive_keeps_the_job_with_the_agent(self):
        job = self.uploaded()
        token = self.claim("analyze")["token"]
        self.quiet(job, 4)

        self.call("alive", {"token": token})
        PrintJob.objects.filter(pk=job.pk).update(heard_at=F("heard_at") - timedelta(minutes=4))

        self.assertIsNone(self.claim("analyze"))  # без весточки четыре и четыре — восемь минут тишины


class PrintQueueTests(AgentCase):
    def test_jobs_go_in_the_order_print_was_pressed(self):
        later = self.queued(submitted_at=timezone.now())
        earlier = self.queued(submitted_at=timezone.now() - timedelta(minutes=1))

        self.assertEqual(self.claim("print")["uuid"], earlier.uuid.hex)
        later.refresh_from_db()
        self.assertEqual(later.status, Status.QUEUED)

    def test_one_job_per_printer_at_a_time(self):
        other = second_printer()
        self.queued(submitted_at=timezone.now() - timedelta(minutes=2))
        self.queued(submitted_at=timezone.now() - timedelta(minutes=1))
        elsewhere = self.queued(printer=other)

        first, second, third = self.claim("print"), self.claim("print"), self.claim("print")

        self.assertNotEqual(first["uuid"], elsewhere.uuid.hex)
        self.assertEqual(second["uuid"], elsewhere.uuid.hex)
        self.assertIsNone(third)

    def test_the_job_brings_what_to_print(self):
        job = self.queued()

        given = self.claim("print")

        self.assertEqual(given["title"], f"knt-{job.uuid.hex}")
        self.assertEqual((given["pdf"], given["selection"], given["copies"], given["duplex"]), ("https://r2/get", "1-2", 2, True))

    def test_an_inactive_printer_gets_nothing(self):
        self.queued(printer=second_printer(active=False))

        self.assertIsNone(self.claim("print"))

    def test_a_canceled_job_is_not_printed(self):
        services.cancel(self.queued())

        self.assertIsNone(self.claim("print"))

    def test_silence_before_sent_puts_the_job_back(self):
        job = self.queued()
        self.claim("print")
        self.quiet(job, 3)

        again = self.claim("print")

        self.assertEqual(again["uuid"], job.uuid.hex)
        job.refresh_from_db()
        self.assertEqual(job.attempts, 2)

    def test_silence_after_sent_is_unknown_and_never_printed_again(self):
        job = self.queued()
        token = self.claim("print")["token"]
        self.call("sent", {"token": token, "cups_job": 17})
        self.quiet(job, 31)

        self.assertIsNone(self.claim("print"))

        job.refresh_from_db()
        self.assertEqual((job.status, job.cups_job), (Status.UNKNOWN, 17))
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(services.used(self.user), 0)

    def test_a_job_never_handed_to_cups_goes_back_in_line_however_long_the_silence(self):
        """Не отдано в CUPS — значит и не напечатано: такое честно печатать заново, а не
        пугать человека «неизвестно»."""
        job = self.queued()
        self.claim("print")
        self.quiet(job, 31)

        self.assertEqual(self.claim("print")["uuid"], job.uuid.hex)

    def test_a_print_token_does_not_report_an_analysis(self):
        self.queued()
        token = self.claim("print")["token"]

        self.assertEqual(self.call("analyzed", {"token": token, "pages": 1, "coverage": [0.1]}).status_code, 409)

    def test_a_long_wait_with_news_is_not_unknown(self):
        """Кончилась бумага: агент ждёт и отчитывается — задание честно ждёт вместе с ним."""
        job = self.queued()
        token = self.claim("print")["token"]
        self.call("sent", {"token": token, "cups_job": 17})
        self.quiet(job, 29)

        self.claim("print")

        job.refresh_from_db()
        self.assertEqual(job.status, Status.PRINTING)

    def test_after_sent_release_is_refused(self):
        job = self.queued()
        token = self.claim("print")["token"]
        self.call("sent", {"token": token, "cups_job": 17})

        answer = self.call("release", {"token": token})

        self.assertEqual(answer.status_code, 409)
        job.refresh_from_db()
        self.assertEqual(job.status, Status.PRINTING)

    def test_before_sent_release_puts_it_back(self):
        job = self.queued()
        token = self.claim("print")["token"]

        self.call("release", {"token": token})

        job.refresh_from_db()
        self.assertEqual(job.status, Status.QUEUED)

    def test_the_cups_number_is_checked(self):
        self.queued()
        token = self.claim("print")["token"]

        for wrong in ("17", -1, True, None):
            with self.subTest(wrong=wrong):
                self.assertEqual(self.call("sent", {"token": token, "cups_job": wrong}).status_code, 400)

    def test_printed_without_the_counter_moving_is_unknown(self):
        job = self.queued()
        token = self.claim("print")["token"]
        self.call("sent", {"token": token, "cups_job": 17})

        self.call("printed", {"token": token, "sure": False})

        job.refresh_from_db()
        self.assertEqual(job.status, Status.UNKNOWN)
        self.assertIn("лоток", job.reason)
        self.assertEqual(services.used(self.user), 0)

    def test_printed_is_done(self):
        job = self.queued()
        token = self.claim("print")["token"]
        self.call("sent", {"token": token, "cups_job": 17})

        self.call("printed", {"token": token})

        job.refresh_from_db()
        self.assertEqual(job.status, Status.DONE)
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(services.used(self.user), 4)

    def test_a_failed_print_gives_the_pages_back(self):
        job = self.queued()
        token = self.claim("print")["token"]

        self.call("fail", {"token": token, "error": "замятие бумаги"})

        job.refresh_from_db()
        self.assertEqual((job.status, job.reason), (Status.FAILED, "замятие бумаги"))
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(services.used(self.user), 0)

    def test_a_failed_print_lands_in_the_failure_journal(self):
        job = self.queued()
        token = self.claim("print")["token"]

        self.call("fail", {"token": token, "error": "замятие бумаги"})

        failure = Failure.objects.get()
        self.assertEqual(failure.source, Failure.Source.PRINTING)
        for part in (job.name, self.user.full_name, "замятие бумаги"):
            self.assertIn(part, failure.text)

    def test_alive_brings_the_panel_text_to_the_student(self):
        self.queued()
        token = self.claim("print")["token"]

        self.call("alive", {"token": token, "panel": "Добавьте бумагу в кассету 1"})

        self.printer.refresh_from_db()
        self.assertEqual(self.printer.state_text, "Добавьте бумагу в кассету 1")

    def test_a_print_the_agent_keeps_dropping_fails(self):
        job = self.queued(tries=queue.PRINT_TRIES)

        self.assertIsNone(self.claim("print"))
        job.refresh_from_db()
        self.assertEqual(job.status, Status.FAILED)
        self.assertIn(job.name, Failure.objects.get(source=Failure.Source.PRINTING).text)

    def test_a_job_marked_failed_in_the_admin_is_closed_for_the_agent(self):
        job = self.queued()
        token = self.claim("print")["token"]
        PrintJob.objects.filter(pk=job.pk).update(status=Status.FAILED)

        self.assertEqual(self.call("printed", {"token": token}).status_code, 409)


class PrinterStatusTests(AgentCase):
    def report(self, **item):
        return self.call("status", {"printers": [{"id": self.printer.pk, **item}]})

    def test_the_card_shows_what_the_agent_saw(self):
        Printer.objects.filter(pk=self.printer.pk).update(seen_at=None)

        self.report(state="no_paper", panel="Добавьте бумагу", toner=55, counter=97700)

        self.printer.refresh_from_db()
        self.assertEqual(
            (self.printer.state, self.printer.state_text, self.printer.toner_percent, self.printer.page_counter),
            ("no_paper", "Добавьте бумагу", 55, 97700),
        )
        self.assertEqual(self.printer.condition(), ("Нет бумаги", "bad"))

    def test_readings_are_written_only_when_something_changes(self):
        self.report(state="ready", toner=55, counter=97700)
        self.report(state="ready", toner=55, counter=97700)
        self.report(state="ready", toner=55, counter=97702)

        self.assertEqual(list(self.printer.readings.values_list("page_counter", flat=True)), [97702, 97700])

    def test_garbage_is_not_trusted(self):
        answer = self.call("status", {"printers": [
            {"id": self.printer.pk, "state": "в огне", "toner": 150, "counter": -1}, {"id": 999999}, "не принтер",
        ]})

        self.assertEqual(answer.status_code, 200)
        self.printer.refresh_from_db()
        self.assertEqual((self.printer.state, self.printer.toner_percent, self.printer.page_counter), ("unknown", None, None))


class CleanupTests(TestCase):
    """Ночная уборка: файлы живут сутки после последнего события задания."""

    @classmethod
    def setUpTestData(cls):
        cls.printer = make_printer()
        cls.user = make_user("p@t.local")

    def setUp(self):
        # Хранилище у тестов одно на весь прогон, а база откатывается после каждого:
        # папки чужих тестов здесь — бесхозные, и уборка честно бы их нашла.
        drop_prefix(cleanup.ROOT)

    def job(self, status, age, **extra):
        """Задание с файлами в хранилище; всё в нём случилось age назад."""
        moment = timezone.now() - age
        job = make_job(self.user, self.printer, status=status, **{"created": moment, "analyzed_at": moment, **extra})
        for name in ("Конспект.pdf", "print.pdf", "pages/0001.jpg"):
            file_storage().save(f"{job.folder}{name}", ContentFile(b"x"))
        return job

    def age_files(self, folder, age):
        moment = (timezone.now() - age).timestamp()
        for key in under(folder.rstrip("/")):
            os.utime(file_storage().path(key), (moment, moment))

    def sweep(self, apply=True):
        with self.captureOnCommitCallbacks(execute=True):
            return cleanup.sweep(apply=apply, say=lambda line: None)

    def kept(self, job):
        job.refresh_from_db()
        return not job.files_dropped and file_storage().exists(f"{job.folder}print.pdf")

    def test_a_job_left_unprinted_for_a_day_expires_and_loses_its_files(self):
        job = self.job(Status.READY, timedelta(days=2))

        self.assertEqual(self.sweep(), (1, 1, 0))

        job.refresh_from_db()
        self.assertEqual((job.status, job.files_dropped), (Status.EXPIRED, True))
        self.assertFalse(file_storage().exists(job.source_key))

    def test_fresh_work_is_left_alone(self):
        jobs = [
            self.job(Status.READY, timedelta(hours=20)),
            # загружено давно, но агент лежал и разобрал только что — сутки идут от разбора
            self.job(Status.READY, timedelta(days=3), analyzed_at=timezone.now() - timedelta(hours=2)),
            self.job(Status.DONE, timedelta(days=3), finished_at=timezone.now() - timedelta(hours=3)),
        ]

        self.assertEqual(self.sweep(), (0, 0, 0))
        for job in jobs:
            self.assertTrue(self.kept(job))

    def test_a_printed_job_loses_its_files_a_day_later(self):
        job = self.job(Status.DONE, timedelta(days=3), finished_at=timezone.now() - timedelta(days=2))

        self.sweep()

        self.assertFalse(self.kept(job))

    def test_a_job_in_the_queue_is_never_swept_however_old(self):
        """Агент лежит неделю — задание ждёт его вместе с файлом."""
        job = self.job(Status.QUEUED, timedelta(days=5), submitted_at=timezone.now() - timedelta(days=5))
        self.age_files(job.folder, timedelta(days=5))

        self.sweep()

        self.assertTrue(self.kept(job))

    def test_a_copy_waiting_for_print_keeps_the_shared_folder(self):
        done = self.job(Status.DONE, timedelta(days=3), finished_at=timezone.now() - timedelta(days=2))
        copy = services.again(done)

        self.sweep()

        self.assertTrue(self.kept(done))
        copy.refresh_from_db()
        self.assertEqual(copy.status, Status.READY)  # разбор у копии старый, но ждать она начала только что
        PrintJob.objects.filter(pk=copy.pk).update(created=timezone.now() - timedelta(days=2))
        self.sweep()
        self.assertFalse(self.kept(done))
        copy.refresh_from_db()
        self.assertEqual((copy.status, copy.files_dropped), (Status.EXPIRED, True))

    def stray(self, age):
        key = f"printing/{uuid4().hex}/Брошенный.pdf"
        file_storage().save(key, ContentFile(b"x"))
        moment = (timezone.now() - age).timestamp()
        os.utime(file_storage().path(key), (moment, moment))
        return key

    def test_a_folder_nobody_owns_is_swept_once_it_is_old(self):
        old, fresh = self.stray(timedelta(days=2)), self.stray(timedelta(hours=1))

        self.assertEqual(self.sweep(), (0, 0, 1))

        self.assertFalse(file_storage().exists(old))
        self.assertTrue(file_storage().exists(fresh))

    def test_a_folder_whose_removal_failed_is_swept_the_next_night(self):
        job = self.job(Status.DONE, timedelta(days=3), finished_at=timezone.now() - timedelta(days=2))
        with mock.patch("printing.cleanup.drop_prefix"):
            self.sweep()
        self.age_files(job.folder, timedelta(days=2))

        self.assertEqual(self.sweep(), (0, 0, 1))
        self.assertFalse(file_storage().exists(f"{job.folder}print.pdf"))

    def test_without_apply_nothing_changes(self):
        job = self.job(Status.READY, timedelta(days=2))
        key = self.stray(timedelta(days=2))

        self.assertEqual(self.sweep(apply=False), (1, 1, 1))

        job.refresh_from_db()
        self.assertEqual(job.status, Status.READY)
        self.assertTrue(self.kept(job))
        self.assertTrue(file_storage().exists(key))

    def test_the_night_task_runs_the_command_for_real(self):
        job = self.job(Status.READY, timedelta(days=2))

        with self.captureOnCommitCallbacks(execute=True):
            said = tasks.sweep_printing()

        self.assertIn("сделано", said)
        self.assertFalse(self.kept(job))


class AlertTests(TestCase):
    """Телеграм о принтерах: одно сообщение на смену, а не на каждое показание."""

    def setUp(self):
        self.printer = make_printer(place="М4, 2 этаж")
        patch = mock.patch("printing.alerts.notify")
        self.sent = patch.start()
        self.addCleanup(patch.stop)

    def said(self):
        return [call.args[2]["trouble"] for call in self.sent.call_args_list]

    def set(self, **fields):
        Printer.objects.filter(pk=self.printer.pk).update(**fields)
        self.printer.refresh_from_db()

    @override_settings(PRINT_AGENT_TOKEN=TOKEN)
    def test_the_agents_readings_bring_one_message_per_change(self):
        report = lambda state: self.client.post(  # noqa: E731
            reverse("printagent_status"), json.dumps({"printers": [{"id": self.printer.pk, "state": state}]}),
            content_type="application/json", headers={"Authorization": f"Bearer {TOKEN}"},
        )

        for state in ("no_paper", "no_paper", "no_paper", "ready"):
            report(state)

        self.assertEqual(self.said(), ["нет бумаги", ""])

    def test_low_toner_is_told_once(self):
        self.set(toner_percent=7)

        alerts.check(self.printer)
        alerts.check(self.printer)

        self.assertEqual(self.said(), ["тонер заканчивается"])

    def test_a_silent_agent_is_noticed_by_the_minute_task(self):
        self.set(seen_at=timezone.now() - timedelta(minutes=6))

        tasks.watch_printers()
        tasks.watch_printers()

        self.assertEqual(self.said(), ["агент печати молчит — печать стоит"])

    def test_a_printer_never_heard_from_is_not_an_alarm(self):
        """Принтер только что завели в админке, агента ещё нет — это не поломка."""
        self.set(seen_at=None)

        tasks.watch_printers()

        self.assertFalse(alerts.check(self.printer))
        self.assertEqual(self.said(), [])

    def test_a_printer_switched_off_in_the_admin_stays_quiet(self):
        self.set(active=False, seen_at=timezone.now() - timedelta(hours=1))

        tasks.watch_printers()

        self.assertFalse(alerts.check(self.printer))
        self.assertEqual(self.said(), [])

    def test_the_message_says_what_is_wrong_and_who_waits(self):
        make_job(make_user("p@t.local"), self.printer, status=Status.QUEUED, submitted_at=timezone.now())
        self.set(state="no_paper", state_text="Добавьте бумагу")

        alerts.check(self.printer)
        context = self.sent.call_args.args[2]
        text = render_to_string("telegram/printer.html", context)

        self.assertIn("М4, 2 этаж", text)
        self.assertIn("нет бумаги, в очереди 1 задание", text)
        self.assertIn("На панели: Добавьте бумагу", text)

    def test_the_way_back_is_told_with_what_it_was(self):
        self.set(state="jam")
        alerts.check(self.printer)
        self.set(state="ready")

        alerts.check(self.printer)
        text = render_to_string("telegram/printer.html", self.sent.call_args.args[2])

        self.assertIn("снова в порядке (было: замятие бумаги)", text)

    def test_the_minute_task_settles_what_the_silent_agent_left_at_the_printer(self):
        job = make_job(make_user("p@t.local"), self.printer, status=Status.PRINTING, cups_job=17,
                       heard_at=timezone.now() - timedelta(minutes=31))

        tasks.watch_printers()

        job.refresh_from_db()
        self.assertEqual(job.status, Status.UNKNOWN)

    def test_both_tasks_are_on_the_schedule(self):
        planned = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}

        self.assertLessEqual({"printing.tasks.sweep_printing", "printing.tasks.watch_printers"}, planned)


# Ответ snmpget -Oqv -Ox от Kyocera M2535dn в М4, снят 22 окт 2026 (порядок — print_agent.OIDS).
KYOCERA = ["3", '"00 "', "97654", "7200", "7200", "-3", '"53 6C 65 65 70 69 6E 67 2E 2E 2E 20 20 "']


class FakeSite:
    """Сайт для агента: записывает, куда стучались, и отвечает заготовленным."""

    def __init__(self, **answers):
        self.calls, self.answers = [], answers

    def talk(self, door, body=None):
        self.calls.append((door, body))
        answer = self.answers.get(door, {})
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def tell(self, door, body):
        try:
            return self.talk(door, body)
        except (print_agent.Gone, print_agent.SiteError, OSError):
            return None

    def doors(self):
        return [door for door, _ in self.calls]


class FakeCups:
    def __init__(self, states=(9,), found=None, submit=41):
        self.states, self.found, self.answer = list(states), found, submit
        self.asked = 0

    def last(self):
        return 40

    def submit(self, queue, path, duplex):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def state(self, job):
        # Предохранитель: зациклившееся ожидание (под мутацией — обычное дело) падает, а не висит.
        self.asked += 1
        assert self.asked < 100, "агент спрашивает CUPS без конца"
        return (self.states.pop(0) if len(self.states) > 1 else self.states[0]), "Замятие в кассете 1"

    def find(self, name, after):
        return self.found


class Clock:
    """Время для циклов ожидания: sleep двигает его, а не ждёт."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        assert self.now < 24 * 3600, "ожидание не кончается"


class AgentServiceTests(SimpleTestCase):
    """Служба агента: SNMP, сборка копий, журнал, ожидание счётчика, разговор с сайтом."""

    def setUp(self):
        patch = mock.patch.object(print_agent, "say")  # агент рассказывает о работе в журнал службы
        patch.start()
        self.addCleanup(patch.stop)

    def journal(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return print_agent.Journal(Path(folder.name) / "journal.json")

    def entry(self, **extra):
        return {"token": "t", "name": "knt-u.pdf", "address": "192.168.16.41", "expected": 4, "before": 100,
                "after": 40, "state": "sent", "cups_job": 41, **extra}

    def test_a_real_kyocera_answer_is_read(self):
        self.assertEqual(
            print_agent.reading(KYOCERA),
            {"state": "ready", "panel": "Sleeping...", "toner": 100, "counter": 97654},
        )

    def test_the_error_bits_name_the_trouble(self):
        def state(errors="00", status="3", cassette="-3"):
            lines = list(KYOCERA)
            lines[0], lines[1], lines[5] = status, f'"{errors} "', cassette
            return print_agent.reading(lines)["state"]

        self.assertEqual(state("40"), "no_paper")
        self.assertEqual(state(cassette="0"), "no_paper")
        self.assertEqual(state("C0"), "no_paper")  # «мало бумаги» сверху не мешает
        self.assertEqual(state("04"), "jam")
        self.assertEqual(state("4C"), "jam")  # замятие важнее остального
        self.assertEqual(state("08"), "open")
        self.assertEqual(state("10"), "no_toner")
        self.assertEqual(state("02"), "offline")
        self.assertEqual(state(status="4"), "busy")
        self.assertEqual(state(status="2"), "unknown")

    def test_toner_that_cannot_be_measured_is_no_percent(self):
        lines = list(KYOCERA)
        lines[3] = "-3"
        self.assertIsNone(print_agent.reading(lines)["toner"])
        lines[3], lines[4] = "3600", "0"
        self.assertIsNone(print_agent.reading(lines)["toner"])

    def test_a_printer_that_does_not_answer_is_none(self):
        silent = subprocess.CompletedProcess([], 1, "", "Timeout: No Response from 192.168.16.41.")
        with mock.patch.object(print_agent.subprocess, "run", return_value=silent):
            self.assertIsNone(print_agent.snmp_read("192.168.16.41", "public"))
        with mock.patch.object(print_agent.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(print_agent.snmp_read("192.168.16.41", "public"))

    def test_each_copy_starts_on_a_new_sheet(self):
        P, B = print_agent.PRINT_PDF, print_agent.BLANK_PDF

        self.assertEqual(print_agent.assembly("1-3", 10, 3, True), [(P, "1-3"), (B, "1"), (P, "1-3"), (B, "1"), (P, "1-3")])
        self.assertEqual(print_agent.assembly("1-2", 10, 2, True), [(P, "1-2"), (P, "1-2")])
        self.assertEqual(print_agent.assembly("1-3", 10, 2, False), [(P, "1-3"), (P, "1-3")])
        self.assertEqual(print_agent.assembly("1-3", 10, 1, True), [(P, "1-3")])

    def test_a_selection_outside_the_document_is_not_printed(self):
        with self.assertRaises(BadSelection):
            print_agent.assembly("9-12", 10, 1, False)

    def test_the_journal_keeps_what_is_being_printed(self):
        journal = self.journal()
        self.assertEqual(journal.load(), {})

        journal.put("a", {"state": "sending"})
        journal.put("b", {"state": "sent"})
        journal.drop("a")

        self.assertEqual(journal.load(), {"b": {"state": "sent"}})
        self.assertEqual([p.name for p in journal.path.parent.iterdir()], ["journal.json"])

    def wait(self, readings, before=100, expected=4):
        clock, readings, panels = Clock(), list(readings), []
        read = lambda: readings.pop(0) if len(readings) > 1 else readings[0]  # noqa: E731
        sure = print_agent.wait_counter(read, before, expected, panels.append, clock, clock.sleep)
        return sure, clock.now, panels

    def test_the_counter_decides_that_paper_came_out(self):
        sure, _, _ = self.wait([{"counter": 100}, {"counter": 102}, {"counter": 104}])

        self.assertTrue(sure)

    def test_a_counter_that_stands_still_means_no_paper_came_out(self):
        sure, waited, _ = self.wait([{"counter": 100, "state": "ready"}])

        self.assertFalse(sure)
        self.assertGreater(waited, print_agent.COUNTER_PATIENCE)
        self.assertLess(waited, print_agent.COUNTER_PATIENCE + 2 * print_agent.COUNTER_EVERY)

    def test_while_the_printer_has_trouble_the_wait_goes_on(self):
        stuck = [{"counter": 101, "state": "no_paper", "panel": "Добавьте бумагу"}] * 100
        sure, waited, panels = self.wait([*stuck, {"counter": 104, "state": "busy"}])

        self.assertTrue(sure)
        self.assertGreater(waited, print_agent.COUNTER_PATIENCE)
        self.assertIn("Добавьте бумагу", panels)

    def test_a_growing_counter_buys_more_time(self):
        slow = [{"counter": 100 + step // 30} for step in range(100)]  # +1 каждые 150 секунд
        sure, _, _ = self.wait([*slow, {"counter": 104}])

        self.assertTrue(sure)

    def test_without_a_reading_before_printing_nothing_is_sure(self):
        self.assertFalse(self.wait([{"counter": 999}], before=None)[0])

    def finish(self, site, cups, journal, readings=({"counter": 104},)):
        clock = Clock()
        journal.put("u", self.entry())
        readings = list(readings)
        read = lambda: readings.pop(0) if len(readings) > 1 else readings[0]  # noqa: E731
        print_agent.finish(site, cups, journal, "u", self.entry(), read, lambda **kwargs: None, clock, clock.sleep)

    def test_finish_reports_printed_and_clears_the_journal(self):
        site, journal = FakeSite(), self.journal()

        self.finish(site, FakeCups(states=(5, 5, 9)), journal)

        self.assertEqual(site.doors(), ["sent", "printed"])
        self.assertEqual(site.calls[-1][1], {"token": "t", "sure": True})
        self.assertEqual(journal.load(), {})

    def test_finish_reports_what_cups_said_when_it_gave_up(self):
        site, journal = FakeSite(), self.journal()

        self.finish(site, FakeCups(states=(5, 8)), journal)

        self.assertEqual(site.doors(), ["sent", "fail"])
        self.assertEqual(site.calls[-1][1]["error"], "Замятие в кассете 1")
        self.assertEqual(journal.load(), {})

    def test_finish_is_unsure_when_the_counter_stands(self):
        site = FakeSite()

        self.finish(site, FakeCups(), self.journal(), readings=({"counter": 100, "state": "ready"},))

        self.assertEqual(site.calls[-1][1], {"token": "t", "sure": False})

    def test_a_job_the_site_closed_leaves_the_journal(self):
        journal = self.journal()

        self.finish(FakeSite(printed=print_agent.Gone("закрыто")), FakeCups(), journal)

        self.assertEqual(journal.load(), {})

    def test_an_unanswered_report_stays_in_the_journal(self):
        """Не дошло «напечатано» — запись остаётся и доложится на следующем круге."""
        journal = self.journal()

        with self.assertRaises(OSError):
            self.finish(FakeSite(printed=OSError("сеть")), FakeCups(), journal)

        self.assertIn("u", journal.load())

    def test_after_a_restart_an_unsent_job_goes_back_in_line(self):
        site, journal = FakeSite(), self.journal()
        journal.put("u", self.entry(state="sending"))

        print_agent.recover(site, FakeCups(found=None), journal, lambda entry: None, lambda **kwargs: None)

        self.assertEqual(site.doors(), ["release"])
        self.assertEqual(journal.load(), {})

    def test_after_a_restart_a_job_found_in_cups_is_finished_not_printed_again(self):
        site, journal = FakeSite(), self.journal()
        journal.put("u", self.entry(state="sending", cups_job=None))

        with mock.patch.object(print_agent, "finish") as finish:
            print_agent.recover(site, FakeCups(found=57), journal, lambda entry: None, lambda **kwargs: None)

        self.assertNotIn("release", site.doors())
        self.assertEqual((journal.load()["u"]["state"], journal.load()["u"]["cups_job"]), ("sent", 57))
        finish.assert_called_once()

    def print_once(self, cups, journal, site=None, **patches):
        job = {"uuid": "u" * 32, "token": "t", "queue": "m4-kyocera", "pdf": "https://r2/pdf", "pages": 4,
               "selection": "1-2", "copies": 1, "duplex": False, "sides": 2}
        site = site or FakeSite(claim={"job": job})
        defaults = {"download": mock.DEFAULT, "assemble": mock.DEFAULT, "finish": mock.DEFAULT, "recover": mock.DEFAULT}
        with mock.patch.multiple(print_agent, **{**defaults, **patches}) as mocks:
            result = print_agent.print_once(site, cups, journal, {}, lambda entry: {"counter": 100}, lambda **kwargs: None)
        return site, result, mocks

    def test_stopped_before_the_journal_the_job_goes_back(self):
        site = FakeSite(claim={"job": {"uuid": "u" * 32, "token": "t", "queue": "q", "pdf": "x", "pages": 1,
                                       "selection": "1", "copies": 1, "duplex": False, "sides": 1}})

        with self.assertRaises(KeyboardInterrupt):
            self.print_once(FakeCups(), self.journal(), site=site, download=mock.Mock(side_effect=KeyboardInterrupt))

        self.assertEqual(site.doors(), ["claim", "release"])

    def test_stopped_while_sending_the_job_is_left_to_the_journal(self):
        """Остановка посреди отправки: что ушло в CUPS, решит `recover` — иначе двойная печать."""
        journal = self.journal()
        site = FakeSite(claim={"job": {"uuid": "u" * 32, "token": "t", "queue": "q", "pdf": "x", "pages": 1,
                                       "selection": "1", "copies": 1, "duplex": False, "sides": 1}})

        with self.assertRaises(KeyboardInterrupt):
            self.print_once(FakeCups(submit=KeyboardInterrupt()), journal, site=site)

        self.assertNotIn("release", site.doors())
        self.assertEqual(journal.load()["u" * 32]["state"], "sending")

    def test_a_failed_send_is_settled_by_looking_into_cups(self):
        journal = self.journal()

        _, result, mocks = self.print_once(FakeCups(submit=RuntimeError("cupsd лежит")), journal)

        self.assertFalse(result)
        mocks["recover"].assert_called_once()

    def test_a_sent_job_is_journaled_and_finished(self):
        journal = self.journal()

        _, result, mocks = self.print_once(FakeCups(), journal)

        self.assertTrue(result)
        entry = mocks["finish"].call_args.args[4]
        self.assertEqual((entry["state"], entry["cups_job"], entry["before"], entry["expected"]), ("sent", 41, 100, 2))
        self.assertEqual(journal.load()["u" * 32]["state"], "sent")

    def analysis(self, **patches):
        job = {"uuid": "a" * 32, "token": "t", "name": "Курсовая.docx", "extension": ".docx",
               "source": "https://r2/src", "queue": "m4-kyocera"}
        site = FakeSite(claim={"job": job}, analyzed={
            "urls": {"print.pdf": "https://r2/p", "pages/0001.jpg": "https://r2/1"},
            "types": {"print.pdf": "application/pdf", "pages/0001.jpg": "image/jpeg"},
        })
        report = {"pages": 1, "coverage": [0.05], "sideways": []}
        defaults = {"download": mock.DEFAULT, "printer_ppd": mock.Mock(return_value=""),
                    "analyze": mock.Mock(return_value=report), "put_file": mock.DEFAULT}
        with mock.patch.multiple(print_agent, **{**defaults, **patches}) as mocks:
            result = print_agent.analysis_once(site, 50 * 1024 * 1024)
        return site, result, mocks

    def test_an_analysis_brings_numbers_files_and_commit(self):
        site, result, mocks = self.analysis()

        self.assertTrue(result)
        self.assertEqual(site.doors(), ["claim", "analyzed", "commit"])
        self.assertEqual(site.calls[1][1], {"token": "t", "pages": 1, "coverage": [0.05], "sideways": []})
        self.assertEqual(sorted(call.args[0] for call in mocks["put_file"].call_args_list), ["https://r2/1", "https://r2/p"])

    def test_large_previews_are_uploaded_only_when_drawn(self):
        def drawn(source, work, ppd, beat):
            (work / "large").mkdir()
            (work / "large" / "0001.jpg").write_bytes(b"jpeg")
            return {"pages": 1, "coverage": [0.05], "sideways": []}

        answer = {
            "urls": {"print.pdf": "https://r2/p", "pages/0001.jpg": "https://r2/1"},
            "large": {"large/0001.jpg": "https://r2/L1", "large/0002.jpg": "https://r2/L2"},
            "types": {},
        }
        site = FakeSite(claim={"job": {"uuid": "a" * 32, "token": "t", "name": "x.pdf", "extension": ".pdf",
                                       "source": "s", "queue": "q"}}, analyzed=answer)
        with mock.patch.multiple(print_agent, download=mock.DEFAULT, printer_ppd=mock.Mock(return_value=""),
                                 analyze=mock.Mock(side_effect=drawn), put_file=mock.DEFAULT) as mocks:
            print_agent.analysis_once(site, 50 * 1024 * 1024)

        sent = sorted(call.args[0] for call in mocks["put_file"].call_args_list)
        self.assertEqual(sent, ["https://r2/1", "https://r2/L1", "https://r2/p"])

    def test_a_file_the_analyzer_refuses_is_told_to_the_student(self):
        site, _, _ = self.analysis(analyze=mock.Mock(side_effect=print_agent.Rejected("PDF защищён паролем.")))

        self.assertEqual(site.doors(), ["claim", "fail"])
        self.assertEqual(site.calls[-1][1]["error"], "PDF защищён паролем.")

    def test_our_own_breakdown_returns_the_file_to_the_queue(self):
        site, result, _ = self.analysis(put_file=mock.Mock(side_effect=OSError("хранилище не отвечает")))

        self.assertFalse(result)
        self.assertEqual(site.doors()[-1], "release")
        self.assertNotIn("fail", site.doors())

    def test_on_start_the_agent_waits_for_the_site_instead_of_falling(self):
        """Во время выкатки сайт отвечает 502 — агент ждёт его, а не падает с перезапуском."""
        limits = {"max_file_mb": print_agent.MAX_FILE_MB, "max_doc_pages": print_agent.MAX_DOC_PAGES}

        class Waking(FakeSite):
            url = "https://knt-mipt.ru"
            queue = [print_agent.SiteError("сайт ответил 502"), OSError("нет сети"), {"limits": limits}]

            def talk(self, door, body=None):
                answer = self.queue.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                return answer

        # Служба доходит до первого круга работы — значит, на старте не упала. Круг сразу
        # останавливаем: дальше бесконечный цикл.
        with mock.patch.object(print_agent.time, "sleep") as sleep,                 mock.patch.object(print_agent, "analysis_once", side_effect=KeyboardInterrupt),                 self.assertRaises(KeyboardInterrupt):
            print_agent.serve_analysis(Waking())

        self.assertEqual(sleep.call_count, 2)

    def test_the_sites_answers_become_exceptions(self):
        site = print_agent.Site("https://knt-mipt.ru", "t")

        def refused(code, body):
            error = urllib.error.HTTPError("https://knt-mipt.ru", code, "x", {}, io.BytesIO(body.encode()))
            return mock.patch.object(print_agent.urllib.request, "urlopen", side_effect=error)

        with refused(409, '{"error": "задание закрыто"}'), self.assertRaisesRegex(print_agent.Gone, "закрыто"):
            site.talk("printed", {"token": "t"})
        with refused(502, "<html>Bad Gateway</html>"), self.assertRaisesRegex(print_agent.SiteError, "502"):
            site.talk("claim", {"kind": "print"})

    def test_every_door_the_agent_knocks_on_exists(self):
        """Ручки агент складывает строкой — своего Django у него нет, и опечатка нашлась бы
        только на М4, посреди печати."""
        source = Path(print_agent.__file__).read_text(encoding="utf-8")
        doors = set(re.findall(r'(?:talk|tell)\("([a-z]+)"', source))

        self.assertGreater(len(doors), 8)
        for door in doors:
            with self.subTest(door=door):
                reverse(f"printagent_{door}")
