from datetime import date, timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from comments.models import Comment
from core.models import Subject, Team
from core.tasks import JOURNAL_DAYS, sweep_journals
from intake.models import MediaJob, failed_bake
from lectorium.models import Lecture, Playlist
from library.models import Book
from materials.models import Material
from users.models import User

from . import announcements, services
from .forms import AnnouncementForm
from .models import Announcement, Notification
from .templatetags.notification_tags import ago

Kind = Notification.Kind


def person(email, name="Иван", surname="Иванов"):
    return User.objects.create_user(email=email, name=name, surname=surname, password="pass12345",
                                    must_change_password=False)


class Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = person("author@t.local", "Анна", "Авторова")
        cls.reader = person("reader@t.local", "Артём", "Зубров")
        cls.other = person("other@t.local", "Пётр", "Третьяков")
        cls.moderator = User.objects.create_superuser("mod@t.local", "Мария", "Модерова", password="pass12345")
        subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.material = Material.objects.create(
            title="Билеты по матану", subject=subject, year=2026, status=Material.Status.APPROVED, uploader=cls.author,
        )
        cls.playlist = Playlist.objects.create(
            title="Оптика", subject=subject, uploader=cls.author, status=Playlist.Status.APPROVED,
        )
        cls.lecture = Lecture.objects.create(playlist=cls.playlist, title="Линзы", prefix="lectures/x", duration=60)
        cls.book = Book.objects.create(title="Ландафшиц", uploader=cls.author, status=Book.Status.PENDING)

    def comment(self, who, owner=None, parent=None, text="Вопрос по билету", hide=False):
        owner = owner or self.material
        comment = Comment.objects.create(author=who, parent=parent, text=text, hide_author=hide,
                                         **{owner._meta.model_name: owner})
        services.commented(comment)
        return comment

    def of(self, user):
        return list(Notification.objects.filter(recipient=user))


class CommentNoticeTests(Fixture):
    """Вопросы под материалами: ради них колокольчик и делался."""

    def test_a_comment_under_a_material_reaches_its_author(self):
        comment = self.comment(self.reader)

        notice, = self.of(self.author)
        self.assertEqual((notice.kind, notice.actor, notice.material, notice.comment), (Kind.COMMENT, self.reader, self.material, comment))

    def test_nobody_is_told_about_their_own_doing(self):
        self.comment(self.author)
        self.assertFalse(Notification.objects.exists())

        mine = self.comment(self.reader)
        self.comment(self.reader, parent=mine)

        self.assertEqual(self.of(self.reader), [])

    def test_a_reply_reaches_who_was_answered(self):
        question = self.comment(self.reader)
        self.comment(self.other, parent=question, text="Посмотри в конспекте")

        notice, = self.of(self.reader)
        self.assertEqual((notice.kind, notice.actor), (Kind.REPLY, self.other))

    def test_the_author_answered_under_their_own_material_gets_one_notice_not_two(self):
        own = self.comment(self.author, text="Добавил билеты")
        Notification.objects.all().delete()

        self.comment(self.reader, parent=own, text="Спасибо!")

        notice, = self.of(self.author)
        self.assertEqual(notice.kind, Kind.REPLY)

    def test_a_comment_under_a_lecture_reaches_the_author_of_the_course(self):
        self.comment(self.reader, owner=self.lecture)

        notice, = self.of(self.author)
        self.assertEqual((notice.kind, notice.lecture), (Kind.COMMENT, self.lecture))

    def test_a_course_without_an_author_tells_nobody(self):
        Playlist.objects.update(uploader=None)
        self.lecture.refresh_from_db()

        self.comment(self.reader, owner=self.lecture)

        self.assertFalse(Notification.objects.exists())

    def test_unread_comments_on_one_thing_gather_into_one_line(self):
        self.comment(self.reader)
        self.comment(self.other, text="И мне интересно")
        last = self.comment(self.reader, text="Ну так что?")

        notice, = self.of(self.author)
        self.assertEqual((notice.times, notice.actor, notice.comment), (3, self.reader, last))

    def test_once_read_the_next_comment_starts_a_new_line(self):
        self.comment(self.reader)
        services.read_all(self.author)

        self.comment(self.other)

        self.assertEqual([one.times for one in self.of(self.author)], [1, 1])

    def test_an_anonymous_comment_stays_anonymous(self):
        self.comment(self.reader, hide=True, text="Где взять ответы?")
        self.client.force_login(self.author)

        panel = self.client.get(reverse("notification_panel")).content.decode()

        self.assertIn("Аноним", panel)
        self.assertNotIn("Зубров", panel)

    def test_a_deleted_comment_takes_its_notice_away(self):
        comment = self.comment(self.reader)

        comment.delete()

        self.assertEqual(self.of(self.author), [])

    def test_a_deleted_comment_leaves_the_rest_of_its_line(self):
        self.comment(self.reader)
        last = self.comment(self.other)

        last.delete()

        notice, = self.of(self.author)
        self.assertEqual((notice.times, notice.comment), (1, None))

    def test_a_comment_posted_through_the_page_notifies(self):
        self.client.force_login(self.reader)

        self.client.post(reverse("comment_add", args=["material", self.material.pk]), {"text": "Есть ответы?"})

        self.assertEqual(len(self.of(self.author)), 1)


class ReviewNoticeTests(Fixture):
    """Решение модератора: автор узнаёт, не заходя на свою страницу."""

    def setUp(self):
        self.client.force_login(self.moderator)

    def test_a_rejection_comes_with_its_reason(self):
        self.client.post(reverse("book_review", args=[self.book.pk]), {"decision": "reject", "note": "Нет файла"})

        notice, = self.of(self.author)
        self.assertEqual((notice.kind, notice.book, notice.note), (Kind.REJECTED, self.book, "Нет файла"))

    def test_each_kind_of_thing_is_announced(self):
        self.client.post(reverse("material_review", args=[self.material.pk]), {"decision": "approve"})
        self.client.post(reverse("book_review", args=[self.book.pk]), {"decision": "approve"})
        self.client.post(reverse("playlist_review", args=[self.playlist.pk]), {"decision": "reject", "note": "?"})

        self.assertEqual(
            sorted((one.thing_kind, one.kind) for one in self.of(self.author)),
            [("book", Kind.APPROVED), ("material", Kind.APPROVED), ("playlist", Kind.REJECTED)],
        )

    def test_a_moderator_deciding_on_their_own_is_not_told(self):
        Material.objects.update(uploader=self.moderator)

        self.client.post(reverse("material_review", args=[self.material.pk]), {"decision": "approve"})

        self.assertFalse(Notification.objects.exists())


class BakeNoticeTests(Fixture):
    """Выпечка лекции — автору курса: сама запись автора не хранит."""

    def setUp(self):
        self.job = MediaJob.objects.create(recipe="lecture", source="uploads/x/a.mkv", lecture=self.lecture)

    def test_a_baked_lecture_is_announced(self):
        services.baked(self.job)

        notice, = self.of(self.author)
        self.assertEqual((notice.kind, notice.lecture), (Kind.BAKED, self.lecture))

    def test_a_failure_comes_with_what_went_wrong(self):
        self.job.note = "ffmpeg не справился"

        failed_bake(self.job)  # туда сходятся оба пути отказа пекарни

        notice, = self.of(self.author)
        self.assertEqual((notice.kind, notice.note), (Kind.BAKE_FAILED, "ffmpeg не справился"))

    def test_a_job_without_a_lecture_tells_nobody(self):
        services.baked(MediaJob(recipe="lecture", source="uploads/x/b.mkv"))

        self.assertFalse(Notification.objects.exists())


class BellTests(Fixture):
    """Колокольчик в шапке, панель и страница."""

    def setUp(self):
        self.client.force_login(self.author)

    def page(self):
        return self.client.get(reverse("bookmark_list")).content.decode()

    def test_the_bell_counts_what_is_unread(self):
        self.comment(self.reader)
        self.book.approve(self.moderator)
        services.reviewed(self.book, self.moderator)

        self.assertInHTML('<span class="inline-flex items-center justify-center min-w-[1.125rem] h-[1.125rem] px-1 '
                          'rounded-full bg-rose-500 text-white text-[10px] font-bold leading-none ring-2 ring-slate-50 '
                          'dark:ring-slate-950">2</span>', self.page())

    def test_the_number_redraws_itself_on_a_reason_from_the_socket(self):
        self.assertIn('hx-trigger="notifications:changed from:body, chats:sync from:body"', self.page())

    def test_with_nothing_unread_there_is_no_number_at_all(self):
        """Спрятанное `hidden` рядом с `inline-flex` проигрывает — так у чата висел «0»."""
        page = self.page()

        self.assertIn('id="notification-badge"', page)
        self.assertNotIn("bg-rose-500 text-white text-[10px]", page)

    def test_a_big_number_is_capped(self):
        Notification.objects.bulk_create(
            Notification(recipient=self.author, kind=Kind.BAKED, lecture=self.lecture) for _ in range(120)
        )

        self.assertIn(">99+<", self.page())

    def test_the_panel_shows_the_latest_with_who_and_what(self):
        self.comment(self.reader, text="А где 14-й билет?")
        self.book.approve(self.moderator)
        services.reviewed(self.book, self.moderator)

        panel = self.client.get(reverse("notification_panel"))

        self.assertContains(panel, "Новый комментарий")
        self.assertContains(panel, "под материалом «Билеты по матану»")
        self.assertContains(panel, "Зубров Артём: «А где 14-й билет?»")
        self.assertContains(panel, "Книга опубликована: «Ландафшиц»")
        self.assertContains(panel, reverse("notification_list"))

    def test_gathered_comments_are_counted_in_words(self):
        for _ in range(3):
            self.comment(self.reader)

        self.assertContains(self.client.get(reverse("notification_panel")), "3 новых комментария")

    def test_a_rejection_shows_its_reason(self):
        self.book.reject(self.moderator, "Нет файла")
        services.reviewed(self.book, self.moderator)

        panel = self.client.get(reverse("notification_panel"))

        self.assertContains(panel, "Книга отклонена: «Ландафшиц»")
        self.assertContains(panel, "Причина: Нет файла")

    def test_opening_a_notice_reads_it_and_goes_to_the_comment(self):
        comment = self.comment(self.reader)
        notice, = self.of(self.author)

        response = self.client.get(reverse("notification_open", args=[notice.pk]))

        self.assertRedirects(response, f"{self.material.get_absolute_url()}#comment-{comment.pk}", fetch_redirect_response=False)
        notice.refresh_from_db()
        self.assertIsNotNone(notice.read_at)

    def test_a_lecture_notice_leads_into_its_course(self):
        comment = self.comment(self.reader, owner=self.lecture)
        notice, = self.of(self.author)

        self.assertEqual(notice.target_url(), f"/lectures/{self.playlist.pk}/?lecture={self.lecture.pk}#comment-{comment.pk}")

    def test_a_stranger_cannot_open_someone_elses_notice(self):
        self.comment(self.reader)
        notice, = self.of(self.author)
        self.client.force_login(self.other)

        self.assertEqual(self.client.get(reverse("notification_open", args=[notice.pk])).status_code, 404)

    def test_read_all_from_the_panel_clears_the_number(self):
        self.comment(self.reader)

        response = self.client.post(reverse("notification_read_all"), headers={"HX-Request": "true"})

        self.assertFalse(services.unread(self.author).exists())
        self.assertContains(response, 'hx-swap-oob="true"')
        self.assertContains(response, 'id="notification-badge"')

    def test_the_page_lists_everything_and_reads_all(self):
        self.comment(self.reader)
        services.baked(MediaJob(lecture=self.lecture))

        page = self.client.get(reverse("notification_list"))

        self.assertContains(page, "Лекция готова: «Линзы»")
        self.assertContains(page, "Новый комментарий")
        self.assertRedirects(self.client.post(reverse("notification_read_all")), reverse("notification_list"))
        self.assertFalse(services.unread(self.author).exists())


class SeenTests(Fixture):
    """Открыл вещь сам — колокольчик о ней больше не напоминает."""

    def setUp(self):
        self.client.force_login(self.author)

    def test_opening_the_material_reads_its_notices_only(self):
        self.comment(self.reader)
        self.book.approve(self.moderator)
        services.reviewed(self.book, self.moderator)

        self.client.get(self.material.get_absolute_url())

        self.assertEqual([one.kind for one in services.unread(self.author)], [Kind.APPROVED])

    def test_opening_the_book_reads_its_notices(self):
        services.reviewed(self.book, self.moderator)

        self.client.get(self.book.get_absolute_url())

        self.assertFalse(services.unread(self.author).exists())

    def test_the_course_page_reads_the_course_its_bakes_and_the_open_lecture(self):
        other = Lecture.objects.create(playlist=self.playlist, title="Призмы", prefix="lectures/y", duration=60, order=1)
        services.reviewed(self.playlist, self.moderator)
        services.baked(MediaJob(lecture=other))
        self.comment(self.reader, owner=self.lecture)
        under_other = self.comment(self.reader, owner=other)

        self.client.get(f"{self.playlist.get_absolute_url()}?lecture={self.lecture.pk}")

        left, = services.unread(self.author)
        self.assertEqual(left.comment, under_other)

    def test_someone_else_opening_the_page_reads_nothing(self):
        self.comment(self.reader)
        self.client.force_login(self.other)

        self.client.get(self.material.get_absolute_url())

        self.assertTrue(services.unread(self.author).exists())


class RingTests(Fixture):
    """Любая перемена звонит по сокету человека — его вкладки переспрашивают число."""

    def rings(self, action):
        with mock.patch("notifications.services.events.notify_notifications") as rung,                 self.captureOnCommitCallbacks(execute=True):
            action()
        return [pk for call in rung.call_args_list for pk in call.args]

    def test_something_new_rings_whom_it_is_for(self):
        self.assertEqual(self.rings(lambda: self.comment(self.reader)), [self.author.pk])
        self.assertEqual(self.rings(lambda: self.comment(self.other)), [self.author.pk])  # и склеенное
        self.assertEqual(self.rings(lambda: services.reviewed(self.book, self.moderator)), [self.author.pk])
        self.assertEqual(self.rings(lambda: services.baked(MediaJob(lecture=self.lecture))), [self.author.pk])

    def test_reading_rings_only_when_something_was_unread(self):
        self.assertEqual(self.rings(lambda: services.seen(self.author, material=self.material)), [])

        self.comment(self.reader)

        self.assertEqual(self.rings(lambda: services.seen(self.author, material=self.material)), [self.author.pk])

    def test_opening_rings_once(self):
        self.comment(self.reader)
        notice, = self.of(self.author)

        self.assertEqual(self.rings(lambda: services.opened(notice)), [self.author.pk])
        self.assertEqual(self.rings(lambda: services.opened(notice)), [])

    def test_a_deleted_comment_rings_who_lost_a_notice(self):
        comment = self.comment(self.reader)

        self.assertEqual(self.rings(comment.delete), [self.author.pk])

    def test_nothing_rings_before_the_row_is_there(self):
        """Вкладка спросила бы число раньше, чем строка появилась, — и не увидела бы её."""
        with mock.patch("notifications.services.events.notify_notifications") as rung:
            self.comment(self.reader)
        rung.assert_not_called()

    def test_a_dead_channel_layer_does_not_take_the_comment_down(self):
        self.client.force_login(self.reader)

        # Журнал снаружи: звонок уходит при выходе из перехвата коммита, позже запроса.
        with self.assertLogs("notifications.services", "ERROR"),                 mock.patch("notifications.services.events.notify_notifications", side_effect=OSError("нет Redis")),                 self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("comment_add", args=["material", self.material.pk]), {"text": "?"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Comment.objects.exists())


def team(number, course):
    """Группа, которая сегодня на `course` курсе (курс выводится из года зачисления)."""
    today = date.today()
    year = today.year - course + (1 if today.month >= 9 else 0)
    return Team.objects.create(number=number, profile="ПМИ", course_code="01.03.02", stage="bachelor",
                               year_of_admission=year)


class AnnouncementTests(Fixture):
    """Объявления из Пульса: всем, курсам, группам — каждому строкой в колокольчик."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second = team("Б05-201", 2)
        cls.second_b = team("Б05-202", 2)
        cls.third = team("Б05-301", 3)
        for user, group in ((cls.author, cls.second), (cls.reader, cls.second_b), (cls.other, cls.third)):
            user.team = group
            user.save()  # не update(): членство в чатах курса ставит сигнал на save
        cls.gone = person("gone@t.local")
        cls.gone.is_active = False
        cls.gone.save()

    def send(self, text="Завтра субботник", link="", courses=(), teams=()):
        with mock.patch("notifications.services.events.notify_notifications") as rung,                 self.captureOnCommitCallbacks(execute=True):
            announcement, count = announcements.publish(
                self.moderator, {"text": text, "link": link, "courses": list(courses), "teams": list(teams)},
            )
        self.rung = sorted(pk for call in rung.call_args_list for pk in call.args)
        return announcement, count

    def test_to_everyone_but_the_sender_and_closed_accounts(self):
        announcement, count = self.send()

        expected = sorted([self.author.pk, self.reader.pk, self.other.pk])
        self.assertEqual(sorted(Notification.objects.values_list("recipient_id", flat=True)), expected)
        self.assertEqual((count, announcement.audience), (3, "всем"))
        self.assertEqual(self.rung, expected)

    def test_to_a_course_is_every_group_of_it(self):
        announcement, _ = self.send(courses=["2"])

        self.assertEqual(sorted(Notification.objects.values_list("recipient_id", flat=True)),
                         sorted([self.author.pk, self.reader.pk]))
        self.assertEqual(announcement.audience, "2 курс")

    def test_courses_and_groups_add_up(self):
        announcement, count = self.send(courses=["3"], teams=[self.second])

        self.assertEqual(count, 2)
        self.assertEqual(announcement.audience, "3 курс, группа Б05-201")

    def test_it_lands_in_the_bell_and_opens_whole(self):
        announcement, _ = self.send(text="**Субботник** в субботу, сбор у М4. Берите перчатки и хорошее настроение.",
                                    link="https://vk.com/knt_mipt")
        self.client.force_login(self.author)

        panel = self.client.get(reverse("notification_panel"))
        self.assertContains(panel, "Объявление: Субботник в субботу")
        self.assertContains(panel, "Модерова Мария")

        notice, = self.of(self.author)
        page = self.client.get(reverse("notification_open", args=[notice.pk]), follow=True)
        self.assertContains(page, "<strong>Субботник</strong>", html=True)
        self.assertContains(page, 'href="https://vk.com/knt_mipt" class="btn btn-primary" target="_blank" rel="noopener"')
        notice.refresh_from_db()
        self.assertIsNotNone(notice.read_at)

    def test_a_link_on_the_site_opens_in_place(self):
        announcement, _ = self.send(link="/materials/")
        self.client.force_login(self.author)

        page = self.client.get(announcement.get_absolute_url()).content.decode()

        self.assertIn('href="/materials/" class="btn btn-primary">', page)

    def test_opening_the_page_reads_it(self):
        announcement, _ = self.send()
        self.client.force_login(self.reader)

        self.client.get(announcement.get_absolute_url())

        self.assertFalse(services.unread(self.reader).exists())

    def test_whoever_it_was_not_sent_to_does_not_see_it(self):
        announcement, _ = self.send(teams=[self.third])
        self.client.force_login(self.reader)

        self.assertEqual(self.client.get(announcement.get_absolute_url()).status_code, 404)

    def test_withdrawing_takes_it_from_everyone_and_rings_who_had_it_unread(self):
        announcement, _ = self.send()
        services.read_all(self.author)

        with mock.patch("notifications.services.events.notify_notifications") as rung,                 self.captureOnCommitCallbacks(execute=True):
            services.withdraw(announcement)

        self.assertFalse(Notification.objects.exists())
        self.assertEqual(sorted(pk for call in rung.call_args_list for pk in call.args),
                         sorted([self.reader.pk, self.other.pk]))

    def test_old_announcements_are_swept_with_their_notices(self):
        announcement, _ = self.send()
        Announcement.objects.update(created=timezone.now() - timedelta(days=JOURNAL_DAYS + 1))

        sweep_journals()

        self.assertFalse(Announcement.objects.exists())

    def test_the_link_is_a_page_of_the_site_or_a_full_address(self):
        for link, ok in (("https://vk.com/knt_mipt", True), ("/materials/?subject=3", True), ("", True),
                         ("//evil.example/x", False), ("javascript:alert(1)", False), ("vk.com/knt", False),
                         ("ftp://x.ru/file", False)):
            with self.subTest(link=link):
                form = AnnouncementForm({"text": "Привет", "link": link})
                self.assertEqual(form.is_valid(), ok, form.errors)


class SweepTests(Fixture):
    def test_notices_live_as_long_as_the_journals(self):
        old = Notification.objects.create(recipient=self.author, kind=Kind.BAKED, lecture=self.lecture)
        Notification.objects.filter(pk=old.pk).update(created=timezone.now() - timedelta(days=JOURNAL_DAYS + 1))
        fresh = Notification.objects.create(recipient=self.author, kind=Kind.BAKED, lecture=self.lecture)

        self.assertIn("уведомлений: 1", sweep_journals())

        self.assertEqual(list(Notification.objects.all()), [fresh])


class AgoTests(SimpleTestCase):
    """Время справа в строке — коротко, чтобы не отнимать места у текста."""

    def test_short_forms(self):
        now = timezone.localtime().replace(hour=15, minute=30)
        cases = [
            (now - timedelta(seconds=20), "только что"),
            (now - timedelta(minutes=12), "12 мин"),
            (now - timedelta(hours=3), "3 ч"),
            (now - timedelta(days=1), "вчера"),
            (now.replace(year=now.year - 1, month=3, day=5), f"5 марта {now.year - 1}"),
        ]
        with mock.patch("notifications.templatetags.notification_tags.timezone.localtime",
                        side_effect=lambda moment=None: (moment or now).astimezone(now.tzinfo)):
            for moment, text in cases:
                with self.subTest(text=text):
                    self.assertEqual(ago(moment), text)
