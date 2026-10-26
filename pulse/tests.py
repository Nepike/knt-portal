from datetime import date, timedelta
from unittest import mock
from urllib.parse import parse_qs

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, Permission
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from chats.models import Chat, Membership, Message
from comments.models import Comment
from core.models import Failure, Lockdown, Moderated, Subject
from economy import rewards
from cosmetics.models import CaseOpening, UserItem
from cosmetics.tests import make_case, make_frame
from economy.models import BalanceLog, Wallet
from economy.services import credit, spend
from materials.models import Material
from printing.models import PrintAccount, PrintJob
from printing.tests import make_job, make_printer
from teachers.models import Review, Teacher
from users.models import LoginEvent, User, UserSession
from wall.models import Board, Pixel, Placement, ProtectedArea, WallProfile

from . import actions, feed, summary, system, tokens, wallstats
from .access import GROUP


def make_user(email, *rights, **extra):
    user = User.objects.create_user(
        email=email, name="Иван", surname=email.split("@")[0].capitalize(), password="pass12345",
        must_change_password=False, **extra,
    )
    for right in rights:
        app, codename = right.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=codename))
    return User.objects.get(pk=user.pk)  # свежий: у прежнего закэшированы права


def make_admin(email="admin@t.local", *rights):
    return make_user(email, "core.view_pulse", *rights)


def live_session(user, seen):
    session = Session.objects.create(
        session_key=f"k{user.pk}{seen.timestamp():.0f}"[:40], session_data="", expire_date=timezone.now() + timedelta(days=1),
    )
    return UserSession.objects.create(session=session, user=user, seen=seen, created=seen)


def logs(obj):
    return list(LogEntry.objects.filter(object_id=str(obj.pk)).values_list("change_message", flat=True))


class AccessTests(TestCase):
    """Пульс видит только тот, у кого есть право; группа «Пульс» его даёт."""

    PAGES = ("pulse_home", "pulse_feed", "pulse_people")

    def test_without_the_right_every_page_is_closed(self):
        self.client.force_login(make_user("s@t.local"))

        for name in self.PAGES:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.assertEqual(self.client.get(reverse("pulse_person", args=[1])).status_code, 403)

    def test_the_pulse_group_opens_it(self):
        user = make_user("a@t.local")
        user.groups.add(Group.objects.get(name=GROUP))
        self.client.force_login(user)

        for name in self.PAGES:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("pulse_person", args=[user.pk])).status_code, 200)

    def test_the_menu_shows_the_item_only_with_the_right(self):
        self.client.force_login(make_user("s@t.local"))
        self.assertNotContains(self.client.get(reverse("wallet")), reverse("pulse_home"))

        self.client.force_login(make_admin())
        self.assertContains(self.client.get(reverse("wallet")), reverse("pulse_home"))


class SummaryTests(TestCase):
    """«Сводка» считает каждую цифру только за свой период."""

    def setUp(self):
        self.now = timezone.now()
        self.author = make_user("author@t.local")
        self.teacher = Teacher.objects.create(name="Пётр", surname="Петров")

    def tile(self, label, period="week"):
        return next(tile for tile in summary.tiles(summary.since(period, self.now)) if tile["label"] == label)

    def test_texts_count_only_inside_the_period(self):
        other = Teacher.objects.create(name="Анна", surname="Сидорова")
        Review.objects.create(teacher=self.teacher, author=self.author, text="свежий")
        Review.objects.create(teacher=other, author=self.author, text="старый", created=self.now - timedelta(days=10))

        self.assertEqual(self.tile("Отзывы и комментарии")["value"], 1)
        self.assertEqual(self.tile("Отзывы и комментарии", "month")["value"], 2)

    def test_today_starts_at_midnight_not_a_day_ago(self):
        midnight = timezone.localtime(self.now).replace(hour=0, minute=0, second=0, microsecond=0)

        self.assertEqual(summary.since("day", self.now), midnight)

    def test_who_comes_by(self):
        make_user("today@t.local", last_login=self.now)
        make_user("lately@t.local", last_login=self.now - timedelta(days=10))
        make_user("long@t.local", last_login=self.now - timedelta(days=90))
        make_user("off@t.local", last_login=self.now, is_active=False)
        live_session(make_user("browsing@t.local", last_login=self.now - timedelta(days=40)), self.now)

        visits = summary.visits(self.now)

        self.assertEqual((visits["today"], visits["week"], visits["month"]), (2, 2, 3))
        # author — ни разу не входил; выключенный не считается нигде.
        self.assertEqual((visits["never"], visits["away"], visits["total"]), (1, 1, 5))

    def test_the_logins_tile_says_since_when_the_journal_is_kept(self):
        self.assertEqual(self.tile("Входы")["note"], "журнал входов пока пуст")

        LoginEvent.objects.create(user=self.author, kind=LoginEvent.Kind.LOGIN)
        self.assertIn("журнал ведётся с", self.tile("Входы")["note"])

        LoginEvent.objects.update(created=self.now - timedelta(days=20))
        LoginEvent.objects.create(user=self.author, kind=LoginEvent.Kind.FAILED)
        self.assertEqual(self.tile("Входы")["note"], "и 1 неудачная попытка")

    def test_a_deadline_close_by_needs_attention_and_a_far_one_does_not(self):
        lock = Lockdown.load()
        lock.deadline = timezone.localdate() + timedelta(days=summary.DEADLINE_SOON + 1)
        lock.save()
        self.assertNotIn("Оплата", str(summary.attention(self.now)))

        Lockdown.objects.update(deadline=timezone.localdate() + timedelta(days=3))
        self.assertIn("через 3 дня", str(summary.attention(self.now)))

        Lockdown.objects.update(active=True)
        self.assertIn("Сайт закрыт заслоном", str(summary.attention(self.now)))

    def test_a_silent_printer_and_fresh_failures_need_attention(self):
        make_printer(seen_at=self.now - timedelta(hours=2))
        Failure.objects.create(source=Failure.Source.MAIL, text="отказ", times=3)

        items = {item["text"]: item for item in summary.attention(self.now - timedelta(days=7))}

        self.assertEqual(items["Kyocera ECOSYS M2535dn"]["tone"], "bad")
        self.assertEqual((items["Сбои"]["tone"], items["Сбои"]["state"]), ("bad", "почта 3"))

    def test_online_means_a_request_within_a_quarter_of_an_hour(self):
        live_session(make_user("here@t.local"), self.now - timedelta(minutes=3))
        live_session(make_user("gone@t.local"), self.now - timedelta(minutes=20))

        self.assertEqual([(person.email, seen) for person, seen in summary.online(self.now)], [("here@t.local", "3 мин назад")])

    def test_the_page_shows_fresh_texts_with_their_place(self):
        Review.objects.create(teacher=self.teacher, author=self.author, text="Объясняет понятно")
        self.client.force_login(make_admin())

        response = self.client.get(reverse("pulse_home"))

        self.assertContains(response, "Объясняет понятно")
        self.assertContains(response, self.teacher.get_absolute_url())
        self.assertContains(response, reverse("pulse_person", args=[self.author.pk]))


class FeedTests(TestCase):
    """Лента: события всех видов, порции, отбор и действия."""

    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("author@t.local")
        cls.teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")

    def material(self, **extra):
        return Material.objects.create(
            title="Механика", subject=self.subject, year=2025, uploader=self.author, **extra,
        )

    def test_every_kind_shows_up(self):
        boss = make_admin()
        now = timezone.now()
        LoginEvent.objects.create(user=self.author, kind=LoginEvent.Kind.LOGIN)
        Review.objects.create(teacher=self.teacher, author=self.author, text="текст")
        material = self.material(status=Moderated.Status.APPROVED, reviewed_by=boss, reviewed_at=now)
        Comment.objects.create(material=material, author=self.author, text="вопрос")
        credit(self.author, 100, BalanceLog.Reason.MANUAL, note="приз", by=boss)
        make_job(self.author, make_printer(), status=PrintJob.Status.DONE, submitted_at=now, sides=3, cost=285)
        Placement.objects.create(board=Board.objects.create(title="Стена"), x=1, y=1, color=1, user=self.author)

        kinds = {event.kind for event in feed.events(timezone.now() + timedelta(seconds=1))}

        self.assertEqual(kinds, {"login", "review", "comment", "upload", "moderation", "wallet", "print", "wall"})

    def test_a_manual_grant_says_who_made_it(self):
        credit(self.author, 100, BalanceLog.Reason.MANUAL, note="приз", by=make_admin())

        event = feed.wallet(timezone.now() + timedelta(seconds=1), 10, None)[0]

        self.assertEqual(event.target, "+100")
        self.assertIn("провёл(а) Admin Иван", event.meta)

    def test_a_picture_comes_along_and_bare_scores_are_named(self):
        """Отзыв из одной картинки или из одних оценок не должен выглядеть пустой строкой."""
        other = Teacher.objects.create(name="Анна", surname="Сидорова")
        Review.objects.create(teacher=self.teacher, author=self.author, image=SimpleUploadedFile("доска.png", b"png"))
        Review.objects.create(teacher=other, author=self.author, score_knowledge=4)
        self.client.force_login(make_admin())

        response = self.client.get(reverse("pulse_feed"), {"kind": "review"})

        pictured, bare = sorted(response.context["events"], key=lambda event: not event.image)
        self.assertTrue(pictured.image)
        self.assertContains(response, 'alt="Картинка к тексту"', count=1)
        self.assertEqual((bare.image, bare.meta), (None, "только оценки"))

    def test_a_comment_on_a_lecture_leads_to_that_lecture(self):
        from intake.tests import make_lecture

        lecture = make_lecture()
        Comment.objects.create(lecture=lecture, author=self.author, text="звук отстаёт")

        event = feed.comments(timezone.now() + timedelta(seconds=1), 10, None)[0]

        self.assertEqual(event.href, f"{reverse('playlist_detail', args=[lecture.playlist_id])}?lecture={lecture.pk}")

    def test_wall_moves_are_folded_by_person_and_hour(self):
        board = Board.objects.create(title="Стена")
        for x in range(3):
            Placement.objects.create(board=board, x=x, y=0, color=1, user=self.author)

        events = feed.wall(timezone.now() + timedelta(seconds=1), 10, None)

        self.assertEqual([event.target for event in events], ["3 клетки"])

    def test_wall_moves_respect_the_cursor(self):
        """Группа ходов приходит один раз — в той порции, куда попал её последний ход."""
        Placement.objects.create(board=Board.objects.create(title="Стена"), x=0, y=0, color=1, user=self.author)

        self.assertEqual(feed.wall(timezone.now() - timedelta(minutes=1), 10, None), [])

    def test_the_feed_comes_in_portions_without_gaps_or_repeats(self):
        start = timezone.now() - timedelta(hours=1)
        for n in range(feed.PAGE + 5):
            LoginEvent.objects.create(user=self.author, kind=LoginEvent.Kind.LOGIN, created=start + timedelta(seconds=n))
        self.client.force_login(make_admin())

        first = self.client.get(reverse("pulse_feed"), {"kind": "login"})
        more = parse_qs(first.context["more"])
        rest = self.client.get(reverse("pulse_feed"), {key: value[0] for key, value in more.items()}, headers={"HX-Request": "true"})

        shown = [event.when for event in first.context["events"]] + [event.when for event in rest.context["events"]]
        everything = LoginEvent.objects.count()  # и вход самого смотрящего: force_login его тоже пишет
        self.assertEqual(len(shown), everything)
        self.assertEqual(len(set(shown)), everything)
        self.assertEqual(rest.context["more"], "")
        # День тот же — заголовок над второй порцией не повторяется.
        self.assertFalse(rest.context["events"][0].new_day)
        self.assertTemplateNotUsed(rest, "pulse/feed.html")

    def test_the_feed_narrows_to_one_kind_and_one_person(self):
        other = make_user("other@t.local")
        Review.objects.create(teacher=self.teacher, author=self.author, text="мой")
        Review.objects.create(teacher=self.teacher, author=other, text="чужой")
        LoginEvent.objects.create(user=self.author, kind=LoginEvent.Kind.LOGIN)
        self.client.force_login(make_admin())

        events = self.client.get(reverse("pulse_feed"), {"kind": "review", "person": self.author.pk}).context["events"]

        self.assertEqual([(event.kind, event.quote) for event in events], [("review", "мой")])

    def test_removing_a_review_from_pulse_costs_the_author_nothing(self):
        review = Review.objects.create(teacher=self.teacher, author=self.author, text="текст")
        rewards.sync(self.author)
        self.client.force_login(make_admin("boss@t.local", "teachers.delete_review"))

        response = self.client.post(reverse("pulse_remove", args=["review", review.pk]), headers={"HX-Request": "true"})

        self.assertContains(response, "Удалено")
        self.assertFalse(Review.objects.filter(pk=review.pk).exists())
        self.assertEqual(self.author.wallet.entries.filter(amount__lt=0).count(), 0)
        self.assertEqual(logs(review), ["Пульс: удалён отзыв"])

    def test_removing_needs_the_same_right_as_on_the_site(self):
        review = Review.objects.create(teacher=self.teacher, author=self.author, text="текст")
        self.client.force_login(make_admin())

        self.assertEqual(self.client.post(reverse("pulse_remove", args=["review", review.pk])).status_code, 403)
        self.assertTrue(Review.objects.filter(pk=review.pk).exists())

    def test_a_published_work_goes_back_to_review(self):
        material = self.material(status=Moderated.Status.APPROVED)
        self.client.force_login(make_admin("boss@t.local", "materials.change_material"))

        self.client.post(reverse("pulse_unpublish", args=["material", material.pk]), headers={"HX-Request": "true"})

        material.refresh_from_db()
        self.assertEqual(material.status, Moderated.Status.PENDING)
        self.assertEqual(logs(material), ["Пульс: возвращено на проверку"])

    def test_actions_are_offered_only_to_whoever_may_do_them(self):
        Review.objects.create(teacher=self.teacher, author=self.author, text="текст")
        self.material(status=Moderated.Status.APPROVED)
        self.material(status=Moderated.Status.PENDING)
        now = timezone.now() + timedelta(seconds=1)

        def offered(user):
            events = actions.offered(feed.events(now, ["review", "upload"]), user)
            return sorted(action.label for event in events for action in event.actions)

        self.assertEqual(offered(make_admin()), [])
        everything = make_admin("boss@t.local", "teachers.delete_review", "materials.change_material")
        # Вернуть на проверку можно только опубликованное.
        self.assertEqual(offered(everything), ["Вернуть на проверку", "Удалить"])


class PeopleTests(TestCase):
    """«Люди»: подборки, карточка и действия — каждое со своим правом и записью в историю."""

    def setUp(self):
        self.person = make_user("student@t.local")
        self.boss = make_admin(
            "boss@t.local", "users.change_user", "printing.change_printaccount", "wall.moderate_wall",
            "economy.add_balancelog",
        )
        self.client.force_login(self.boss)

    def act(self, name, person=None, **data):
        return self.client.post(reverse(name, args=[(person or self.person).pk]), data, follow=True)

    def found(self, **query):
        return [person.email for person in self.client.get(reverse("pulse_people"), query).context["page"]]

    def test_presets_pick_the_right_people(self):
        make_user("never@t.local")
        make_user("off@t.local", is_active=False, last_login=timezone.now())
        PrintAccount.objects.create(user=self.person, banned=True)
        self.person.groups.add(Group.objects.get(name="Печать без лимита"))
        User.objects.filter(pk__in=[self.person.pk, self.boss.pk]).update(last_login=timezone.now())

        self.assertEqual(self.found(only="never"), ["never@t.local"])
        self.assertEqual(self.found(only="off"), ["off@t.local"])
        self.assertEqual(self.found(only="banned"), ["student@t.local"])
        self.assertEqual(self.found(only="rights"), ["student@t.local"])

    def test_people_are_found_by_address_too(self):
        self.assertEqual(self.found(q="student@"), ["student@t.local"])

    def test_the_card_counts_messages_but_never_shows_them(self):
        chat = Chat.objects.create(kind="group", title="Свои")
        Membership.objects.create(chat=chat, user=self.person)
        Message.objects.create(chat=chat, author=self.person, text="совершенно секретно")

        response = self.client.get(reverse("pulse_person", args=[self.person.pk]))

        self.assertNotContains(response, "совершенно секретно")
        self.assertContains(response, "сообщения: 1 в 1 чатах")

    def test_printing_is_closed_and_opened(self):
        self.act("pulse_person_printing", banned="1")
        self.assertTrue(PrintAccount.objects.get(user=self.person).banned)

        self.act("pulse_person_printing", banned="0")
        self.assertFalse(PrintAccount.objects.get(user=self.person).banned)
        self.assertEqual(logs(self.person), ["Пульс: печать открыта", "Пульс: печать закрыта"])

    def test_each_action_needs_its_own_right(self):
        self.client.force_login(make_admin("viewer@t.local"))

        response = self.act("pulse_person_printing", banned="1")

        self.assertContains(response, "нужно право")
        self.assertFalse(PrintAccount.objects.filter(user=self.person, banned=True).exists())

    def test_the_wall_is_closed_for_a_week(self):
        self.act("pulse_person_wall", days="7")

        until = WallProfile.objects.get(user=self.person).banned_until
        self.assertAlmostEqual(until, timezone.now() + timedelta(days=7), delta=timedelta(minutes=1))

    def test_a_switched_off_account_loses_its_sessions(self):
        live_session(self.person, timezone.now())

        self.act("pulse_person_active", active="0")

        self.person.refresh_from_db()
        self.assertFalse(self.person.is_active)
        self.assertFalse(UserSession.objects.filter(user=self.person).exists())

    def test_one_cannot_switch_off_oneself(self):
        response = self.act("pulse_person_active", person=self.boss, active="0")

        self.assertContains(response, "Свой аккаунт отсюда не выключить")
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.is_active)

    def test_only_a_superuser_changes_a_superuser(self):
        root = User.objects.create_superuser("root@t.local", "Анна", "Главная", password="pass12345")

        response = self.act("pulse_person_active", person=root, active="0")

        self.assertContains(response, "Суперпользователя меняет только суперпользователь")
        root.refresh_from_db()
        self.assertTrue(root.is_active)

    def test_a_right_is_given_and_taken_back_with_a_direct_permission_too(self):
        unlimited = Permission.objects.get(codename="unlimited")
        self.act("pulse_person_right", group="Печать без лимита", on="1")
        self.assertTrue(self.person.groups.filter(name="Печать без лимита").exists())

        self.person.user_permissions.add(unlimited)
        self.act("pulse_person_right", group="Печать без лимита", on="0")

        self.assertFalse(self.person.groups.filter(name="Печать без лимита").exists())
        self.assertFalse(self.person.user_permissions.filter(pk=unlimited.pk).exists())

    def test_one_cannot_take_away_ones_own_pulse(self):
        self.boss.groups.add(Group.objects.get(name=GROUP))

        response = self.act("pulse_person_right", person=self.boss, group=GROUP, on="0")

        self.assertContains(response, "Своё право на Пульс")
        self.assertTrue(self.boss.groups.filter(name=GROUP).exists())

    def test_tokens_by_hand_say_who_and_why(self):
        self.act("pulse_person_tokens", amount="150", note="приз за конкурс", sign="+")

        entry = BalanceLog.objects.get(wallet__user=self.person)
        self.assertEqual((entry.amount, entry.reason, entry.note, entry.by), (150, "manual", "приз за конкурс", self.boss))

    def test_tokens_cannot_take_a_balance_below_zero(self):
        response = self.act("pulse_person_tokens", amount="10", note="ошибка", sign="-")

        self.assertContains(response, "баланс ушёл бы в минус")
        self.assertFalse(BalanceLog.objects.filter(wallet__user=self.person).exists())

    def test_a_reset_link_goes_to_the_person(self):
        self.act("pulse_person_password", how="link")

        self.assertEqual([letter.to for letter in mail.outbox], [["student@t.local"]])

    def test_sessions_end_one_by_one_or_all_at_once(self):
        first = live_session(self.person, timezone.now())
        live_session(self.person, timezone.now() - timedelta(minutes=1))

        self.act("pulse_person_sessions", id=str(first.pk))
        self.assertEqual(UserSession.objects.filter(user=self.person).count(), 1)

        self.act("pulse_person_sessions")
        self.assertFalse(UserSession.objects.filter(user=self.person).exists())


class TestSessionTests(TestCase):
    """Ключ для приватного окна — посмотреть сайт глазами человека. Только суперпользователю."""

    def setUp(self):
        self.person = make_user("student@t.local")
        self.root = User.objects.create_superuser("root@t.local", "Анна", "Главная", password="pass12345")

    def ask(self, who, minutes="30", person=None):
        self.client.force_login(who)
        return self.client.post(reverse("pulse_person_session", args=[(person or self.person).pk]), {"minutes": minutes})

    def test_the_key_opens_the_site_as_that_person_and_is_written_down(self):
        response = self.ask(self.root)

        key = response.context["key"]
        visitor = self.client_class()
        visitor.cookies[settings.SESSION_COOKIE_NAME] = key
        self.assertEqual(visitor.get(reverse("profile", args=[self.person.pk])).context["user"], self.person)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(logs(self.person), ["Пульс: выдана сессия для проверки на 30 минут"])
        # Хозяин аккаунта видит её в своих устройствах, но не видит, кто её выдал:
        # часть админов студентам не известна.
        self.assertEqual(UserSession.objects.get(session_id=key).where(), "синтетическая сессия")

    def test_it_lives_as_long_as_asked_and_no_longer(self):
        key = self.ask(self.root, "120").context["key"]

        expires = Session.objects.get(session_key=key).expire_date
        self.assertAlmostEqual(expires, timezone.now() + timedelta(minutes=120), delta=timedelta(minutes=1))
        self.assertContains(self.ask(self.root, "600"), "Такого срока нет")

    def test_only_a_superuser_gets_one(self):
        response = self.ask(make_admin("boss@t.local", "users.change_user"))

        self.assertContains(response, "только суперпользователь")
        self.assertFalse(UserSession.objects.filter(user=self.person).exists())

    def test_not_for_a_switched_off_account_nor_for_oneself(self):
        User.objects.filter(pk=self.person.pk).update(is_active=False)

        self.assertContains(self.ask(self.root), "Аккаунт выключен")
        self.assertContains(self.ask(self.root, person=self.root), "Своя сессия у тебя и так есть")
        self.assertFalse(UserSession.objects.filter(agent="синтетическая сессия").exists())


class TokensTests(TestCase):
    """«Токены и магазин»: суммы по причинам, кейсы без раздувания, сверка кошельков."""

    def setUp(self):
        self.person = make_user("student@t.local")
        self.boss = make_admin("boss@t.local", "cosmetics.change_case", "economy.change_wallet")
        self.client.force_login(self.boss)

    def test_where_tokens_come_from_and_where_they_go(self):
        BalanceLog.objects.all().delete()  # вход админа в setUp уже принёс ему стартовые
        credit(self.person, 500, BalanceLog.Reason.WELCOME)
        credit(self.person, 50, BalanceLog.Reason.MATERIAL)
        spend(self.person, 300, BalanceLog.Reason.PURCHASE, note="Пламя")
        spend(self.person, 100, BalanceLog.Reason.CASE)

        data = tokens.overview(timezone.now() - timedelta(days=7))

        self.assertEqual([(row["label"], row["total"]) for row in data["sources"]], [("стартовые", 500), ("материалы", 50)])
        self.assertEqual((data["earned"], data["spent"], data["spent_share"]), (550, 400, 72.7))
        self.assertEqual((data["shop"], data["cases_spent"]), ((300, 1), (100, 1)))

    def test_case_totals_are_not_inflated_by_its_items(self):
        """Соединение и с вещами, и с открытиями сразу перемножило бы строки."""
        case = make_case(items=[make_frame("Пламя"), make_frame("Лёд")])
        for paid in (0, 300, 300):
            CaseOpening.objects.create(user=self.person, case=case, rarity="rare", paid=paid, refund=50)

        row = tokens.cases()[0]

        self.assertEqual((row["size"], row["opened"], row["free"], row["paid"], row["refund"]), (2, 3, 1, 600, 150))

    def test_a_small_sample_is_called_small(self):
        CaseOpening.objects.create(user=self.person, case=make_case(), rarity="common", paid=0)

        data = tokens.rarity()

        self.assertTrue(data["small"])
        self.assertEqual(data["rows"][0]["share"], 100)

    def test_a_case_is_switched_off_and_an_empty_one_is_not_switched_on(self):
        full, empty = make_case("Рамки", items=[make_frame()]), make_case("Пустой", active=False)

        self.client.post(reverse("pulse_case_toggle", args=[full.pk]), {"active": "0"})
        response = self.client.post(reverse("pulse_case_toggle", args=[empty.pk]), {"active": "1"}, follow=True)

        full.refresh_from_db()
        empty.refresh_from_db()
        self.assertEqual((full.active, empty.active), (False, False))
        self.assertContains(response, "пустой кейс открыть нельзя")
        self.assertEqual(logs(full), ["Пульс: кейс закрыт"])

    def test_wallets_off_the_journal_are_shown_and_fixed_only_with_the_right(self):
        credit(self.person, 100, BalanceLog.Reason.MANUAL)
        Wallet.objects.filter(user=self.person).update(balance=999)

        report = self.client.post(reverse("pulse_reconcile"))
        self.assertContains(report, "в кошельке 999, по журналу 100")

        self.client.force_login(make_admin("viewer@t.local"))
        self.assertContains(self.client.post(reverse("pulse_reconcile"), {"apply": "1"}), "нужно право")
        self.assertEqual(Wallet.objects.get(user=self.person).balance, 999)

        self.client.force_login(self.boss)
        self.client.post(reverse("pulse_reconcile"), {"apply": "1"})
        self.assertEqual(Wallet.objects.get(user=self.person).balance, 100)

    def test_the_page_opens(self):
        self.assertEqual(self.client.get(reverse("pulse_tokens")).status_code, 200)


class WallTabTests(TestCase):
    """«Стена» в Пульсе — пока такая, как сама Стена."""

    def setUp(self):
        self.artist = make_user("artist@t.local")
        self.board = Board.objects.create(title="Сезон", width=4, height=4)
        self.boss = make_admin("boss@t.local", "wall.moderate_wall")
        self.client.force_login(self.boss)

    def test_moves_by_day_and_what_is_painted_now(self):
        now = timezone.now()
        Placement.objects.create(board=self.board, x=0, y=0, color=3, user=self.artist)
        Placement.objects.create(board=self.board, x=1, y=0, color=0, user=self.artist)
        Placement.objects.create(board=self.board, x=2, y=0, color=3, user=self.artist, created=now - timedelta(days=3))
        Pixel.objects.create(board=self.board, x=0, y=0, color=3, user=self.artist)
        Pixel.objects.create(board=self.board, x=1, y=0, color=0, user=self.artist)

        data = wallstats.overview(now)

        self.assertEqual((data["moves"], data["erased"], data["painted"], data["cells"]), (3, 1, 1, 16))
        self.assertEqual([day["n"] for day in data["days"]][-4:], [1, 0, 0, 2])
        self.assertEqual(data["top_all"], [(self.artist, 3)])

    def test_the_board_is_opened_for_a_banned_person(self):
        WallProfile.objects.create(user=self.artist, banned_until=timezone.now() + timedelta(days=2))

        self.client.post(reverse("pulse_wall_unban", args=[self.artist.pk]))

        self.assertIsNone(WallProfile.objects.get(user=self.artist).banned_until)
        self.assertEqual(logs(self.artist), ["Пульс: Стена открыта"])

    def test_a_protected_area_is_released_only_by_a_wall_moderator(self):
        area = ProtectedArea.objects.create(board=self.board, x1=0, y1=0, x2=1, y2=1, note="логотип", by=self.boss)
        self.client.force_login(make_admin("viewer@t.local"))

        self.client.post(reverse("pulse_wall_unprotect", args=[area.pk]))
        self.assertTrue(ProtectedArea.objects.filter(pk=area.pk).exists())

        self.client.force_login(self.boss)
        self.client.post(reverse("pulse_wall_unprotect", args=[area.pk]))
        self.assertFalse(ProtectedArea.objects.filter(pk=area.pk).exists())

    def test_without_a_board_the_page_says_so(self):
        Board.objects.update(is_active=False)

        self.assertContains(self.client.get(reverse("pulse_wall")), "Открытой доски нет")


class SystemTabTests(TestCase):
    """«Система»: расписание, заслон, бэкап, очередь."""

    def setUp(self):
        self.root = User.objects.create_superuser("root@t.local", "Анна", "Главная", password="pass12345")
        self.client.force_login(self.root)

    def test_every_scheduled_task_has_a_human_name(self):
        when = {row["label"]: row["when"] for row in system.schedule()}

        self.assertEqual(set(settings.CELERY_BEAT_SCHEDULE) - set(system.LABELS), set())
        self.assertEqual(when["Бэкап базы"], "каждый день в 3:40")
        self.assertEqual(when["Присмотр за принтерами"], "каждую минуту")

    def test_the_lockdown_is_switched_and_written_down(self):
        self.client.post(reverse("pulse_system_lockdown"), {"active": "1"})
        self.assertTrue(Lockdown.load().active)

        self.client.post(reverse("pulse_system_lockdown"), {"active": "0"})
        self.assertFalse(Lockdown.load().active)
        self.assertEqual(logs(Lockdown.load()), ["Пульс: заслон снят", "Пульс: сайт закрыт заслоном"])

    def test_closing_the_site_needs_a_pass_through_it(self):
        """Иначе закрывший остался бы снаружи вместе со всеми."""
        self.client.force_login(make_admin("boss@t.local", "core.change_lockdown"))

        response = self.client.post(reverse("pulse_system_lockdown"), {"active": "1"}, follow=True)

        self.assertContains(response, "Сначала дай себе «Проход при закрытии»")
        self.assertFalse(Lockdown.load().active)

    def test_a_backup_out_of_schedule_is_only_for_a_superuser(self):
        with mock.patch("pulse.system.backup_database.delay") as queued:
            self.client.post(reverse("pulse_system_backup"))
            self.client.force_login(make_admin("boss@t.local"))
            self.client.post(reverse("pulse_system_backup"))

        self.assertEqual(queued.call_count, 1)

    def test_backup_copies_are_listed_on_request(self):
        stored = {date(2026, 10, 25): ("backups/knt-2026-10-25.dump", 1_700_000)}
        with mock.patch("pulse.system.backup.stored", return_value=stored):
            response = self.client.get(reverse("pulse_system_backups"))

        self.assertContains(response, "25 октября 2026")
        self.assertContains(response, "1,6")

    def test_the_queue_check_tells_alive_from_silent(self):
        ask = lambda: self.client.post(reverse("pulse_system_queue"))  # noqa: E731
        with mock.patch("pulse.system.celery_app.connection_for_write"), \
             mock.patch("pulse.system.ping.apply_async") as sent:
            sent.return_value.get.return_value = "pong"
            self.assertContains(ask(), "Воркер ответил")

            # Брокер на месте, а воркер молчит.
            sent.return_value.get.side_effect = TimeoutError
            with self.assertLogs("pulse.system", "WARNING"):
                self.assertContains(ask(), "Очередь не ответила")

    def test_with_the_broker_down_nothing_is_even_sent(self):
        """Отправка при лежащем Redis переподключалась бы минутами — сперва одна попытка соединения."""
        with mock.patch("pulse.system.celery_app.connection_for_write") as connection, \
             mock.patch("pulse.system.ping.apply_async") as sent, self.assertLogs("pulse.system", "WARNING"):
            connection.return_value.__enter__.return_value.ensure_connection.side_effect = OSError("нет Redis")
            self.assertContains(self.client.post(reverse("pulse_system_queue")), "Очередь не ответила")

        sent.assert_not_called()

    @override_settings(SITE_VERSION="3f2fc8f · 2026-10-26 · Print to EVERYONE")
    def test_the_deployed_version_is_shown(self):
        self.assertContains(self.client.get(reverse("pulse_system")), "Print to EVERYONE")

    def test_the_last_run_of_a_task_is_on_the_page(self):
        marks = {"printing.tasks.watch_printers": {"at": timezone.now(), "ok": False, "result": "агент молчит"}}
        with mock.patch("pulse.system.runs.last", side_effect=marks.get):
            response = self.client.get(reverse("pulse_system"))

        self.assertContains(response, "агент молчит")


class GiftTests(TestCase):
    """«Подарить вещь» из карточки: в инвентарь, без токенов, один раз."""

    def setUp(self):
        self.person = make_user("student@t.local")
        self.item = make_frame("Пламя")
        self.client.force_login(make_admin("boss@t.local", "cosmetics.add_useritem"))

    def give(self):
        return self.client.post(reverse("pulse_person_gift", args=[self.person.pk]), {"item": self.item.pk}, follow=True)

    def test_a_gift_lands_in_the_inventory_once(self):
        self.give()
        response = self.give()

        self.assertEqual(UserItem.objects.filter(user=self.person, item=self.item).count(), 1)
        self.assertContains(response, "уже есть в инвентаре")
        self.assertEqual(logs(self.person), ["Пульс: подарена вещь «Пламя»"])
        # Подаренное в списке «что подарить» больше не предлагается.
        self.assertNotIn(self.item, response.context["giftable"])

    def test_a_gift_needs_the_right(self):
        self.client.force_login(make_admin("viewer@t.local"))

        self.give()

        self.assertFalse(UserItem.objects.filter(user=self.person).exists())
