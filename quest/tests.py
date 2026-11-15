import base64
import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.template.loader import render_to_string
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Moderated, Subject
from cosmetics.models import UserItem
from cosmetics.tests import make_frame
from economy import rewards
from economy.models import BalanceLog
from lectorium.models import Lecture, Playlist
from library.models import Book
from users.models import User

from . import keys
from .models import Broadcast, Clue, Find, Fragment, Tally, Unlock
from .templatetags.quest_extras import JOURNAL_STEP, ROUND, VEIL, clues

# Полночь 1 декабря 2026 по Москве — в миллисекундах, как их ждёт браузер.
MIDNIGHT = int(datetime(2026, 12, 1, tzinfo=ZoneInfo("Europe/Moscow")).timestamp() * 1000)


def make_user(email="u@t.local", name="Илья", surname="Смирнов"):
    return User.objects.create_user(email=email, password="pass12345", must_change_password=False, name=name, surname=surname)


class QuestCase(TestCase):
    def setUp(self):
        # Ограничитель, подсказки и числа журнала живут в кэше и пережили бы тест.
        cache.clear()
        self.addCleanup(cache.clear)


@override_settings(SITE_THEME="glitch")
class CountdownTests(QuestCase):
    """Страница отсчёта: на время глитча — главная для всех, и для гостей."""

    def test_a_guest_sees_it_and_search_engines_do_not(self):
        response = self.client.get(reverse("countdown"))

        self.assertContains(response, "./countdown --follow")
        self.assertContains(response, '<meta name="robots" content="noindex">', html=True)
        self.assertContains(response, "terminal('',")
        self.assertContains(response, f"clock({MIDNIGHT},")

    def test_the_root_leads_everyone_to_it(self):
        self.assertRedirects(self.client.get("/"), reverse("countdown"))
        self.client.force_login(make_user())
        self.assertRedirects(self.client.get("/"), reverse("countdown"))

    def test_the_header_of_one_who_is_in_counts_to_the_same_midnight(self):
        self.client.force_login(make_user())

        page = self.client.get(reverse("material_list")).content.decode()

        self.assertIn(f'href="{reverse("countdown")}" class="skin-timer"', page)
        self.assertIn(f"clock({MIDNIGHT})", page)

    def test_the_console_knows_who_is_typing(self):
        self.client.force_login(make_user())

        self.assertContains(self.client.get(reverse("countdown")), "terminal('Смирнов Илья',")

    @override_settings(SITE_THEME="default")
    def test_outside_the_hack_there_is_no_countdown_at_all(self):
        """О событии заранее — ничего: ни страницы, ни ответов консоли."""
        self.assertEqual(self.client.get(reverse("countdown")).status_code, 404)
        self.assertEqual(self.client.post(reverse("countdown_ask"), {"command": "ls"}).status_code, 404)

    @override_settings(SITE_THEME="default")
    def test_outside_the_hack_the_root_goes_where_it_went(self):
        self.assertRedirects(self.client.get("/"), reverse("about"))
        self.client.force_login(make_user())
        self.assertRedirects(self.client.get("/"), reverse("material_list"), fetch_redirect_response=False)


@override_settings(SITE_THEME="glitch")
class AskTests(QuestCase):
    """Консоль спрашивает сайт о командах, на которые у неё нет своего ответа."""

    def ask(self, command):
        return self.client.post(reverse("countdown_ask"), {"command": command})

    def test_a_clue_answers_its_command_whatever_the_case_and_spaces(self):
        Clue.objects.create(place=Clue.Place.COMMAND, command="cat .secret/key", text="первый фрагмент", laugh=True)

        answer = self.ask("  CAT   .Secret/KEY ").json()

        self.assertEqual(answer, {"text": "первый фрагмент", "laugh": True})

    def test_an_unknown_command_gets_nothing(self):
        self.assertEqual(self.ask("rm -rf /").json(), {"text": None})

    def test_a_hidden_clue_stays_silent(self):
        Clue.objects.create(place=Clue.Place.COMMAND, command="ping", text="pong", active=False)

        self.assertEqual(self.ask("ping").json(), {"text": None})

    def test_only_console_clues_answer_commands(self):
        """Подсказку из консоли перенесли в журнал мимо save() — команда у неё осталась,
        но отвечать на неё консоль уже не должна."""
        clue = Clue.objects.create(place=Clue.Place.COMMAND, command="ping", text="строка журнала")
        Clue.objects.filter(pk=clue.pk).update(place=Clue.Place.LOG)

        self.assertEqual(self.ask("ping").json(), {"text": None})

    def test_only_sixty_characters_are_read(self):
        Clue.objects.create(place=Clue.Place.COMMAND, command="x" * 60, text="длинная")

        self.assertEqual(self.ask("x" * 60 + "лишнее").json()["text"], "длинная")

    def test_asking_is_post_only(self):
        self.assertEqual(self.client.get(reverse("countdown_ask")).status_code, 405)

    @patch("quest.views.ASKS_PER_USER", 2)
    def test_one_who_is_in_is_counted_by_account(self):
        self.client.force_login(make_user())

        self.ask("a")
        self.ask("b")
        refused = self.ask("c")

        self.assertEqual(refused.status_code, 429)
        self.assertTrue(refused.json()["warn"])

    @patch("quest.views.ASKS_PER_ADDRESS", 2)
    def test_guests_are_counted_by_address(self):
        self.ask("a")
        self.ask("b")

        self.assertEqual(self.ask("c").status_code, 429)
        self.assertEqual(self.client.post(reverse("countdown_ask"), {"command": "d"}, REMOTE_ADDR="10.0.0.9").status_code, 200)

    def test_every_command_is_counted_and_ones_the_browser_answered_only_counted(self):
        """Для праздничной страницы: команды, на которые браузер ответил сам, приходят
        с пометкой local — их считают, но ни о чём не спрашивают."""
        Clue.objects.create(place=Clue.Place.COMMAND, command="help", text="подсказка")

        self.ask("rm -rf /")
        local = self.client.post(reverse("countdown_ask"), {"command": "help", "local": "1"})

        self.assertEqual(local.json(), {"text": None})
        self.assertEqual(Tally.of(Tally.COMMANDS), 2)

    @patch("quest.views.ASKS_PER_USER", 1)
    def test_a_refused_command_is_not_counted(self):
        self.client.force_login(make_user())
        self.ask("a")

        self.ask("b")

        self.assertEqual(Tally.of(Tally.COMMANDS), 1)

    @patch("quest.views.COUNTED_PER_DAY", 2)
    def test_one_person_counts_only_so_much_a_day(self):
        """Скрипт не накрутит счёт: сверх суточного потолка команды отвечают, но не считаются."""
        self.client.force_login(make_user())
        for command in ("a", "b", "c"):
            self.ask(command)
        other = Client()
        other.force_login(make_user("other@t.local"))
        other.post(reverse("countdown_ask"), {"command": "d"})

        self.assertEqual(Tally.of(Tally.COMMANDS), 3)


class ClueTests(QuestCase):
    def test_a_command_is_kept_plain(self):
        clue = Clue.objects.create(place=Clue.Place.COMMAND, command="  CAT   X ", text="т")

        self.assertEqual(clue.command, "cat x")

    def test_a_clue_outside_the_console_has_no_command(self):
        clue = Clue.objects.create(place=Clue.Place.LOG, command="лишнее", text="т")

        self.assertEqual(clue.command, "")

    def test_a_console_clue_needs_its_command(self):
        with self.assertRaises(ValidationError):
            Clue(place=Clue.Place.COMMAND, text="т").full_clean()

    def test_one_command_has_one_answer(self):
        Clue.objects.create(place=Clue.Place.COMMAND, command="ls .secret", text="раз")

        with self.assertRaises(IntegrityError):
            Clue.objects.create(place=Clue.Place.COMMAND, command="LS  .secret", text="два")

    def test_editing_a_clue_refreshes_what_pages_show(self):
        self.assertEqual(clues()["log"], [])
        clue = Clue.objects.create(place=Clue.Place.LOG, text="новая строка")
        self.assertEqual(clues()["log"], ["новая строка"])
        clue.delete()
        self.assertEqual(clues()["log"], [])

    def test_a_fragment_is_four_to_six_letters_or_digits(self):
        for value in ("7f3", "7f3ab0b", "7f-3a", "7f_3a"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Fragment(number=1, value=value).full_clean()
        for value in ("7f3a", "C4FE", "кнт13"):
            with self.subTest(value=value):
                Fragment(number=1, value=value).full_clean()

    def test_two_fragments_cannot_share_a_value(self):
        """Сдача нашла бы всегда первый из двух — второй не сдать никак."""
        Fragment.objects.create(number=1, value="7f3a")

        with self.assertRaises(ValidationError):
            Fragment(number=2, value="7F3A").full_clean()
        Fragment.objects.get(number=1).full_clean()

    def test_editing_a_fragment_refreshes_the_count(self):
        self.assertEqual(clues()["fragments"], 0)
        fragment = Fragment.objects.create(number=1, value="7f3a")
        self.assertEqual(clues()["fragments"], 1)
        fragment.delete()
        self.assertEqual(clues()["fragments"], 0)


@override_settings(SITE_THEME="glitch")
class KeyTests(QuestCase):
    """Ключ в консоли: каталог key/, битые .frag и сдача копий командой found (quest.keys)."""

    def setUp(self):
        super().setUp()
        self.person = make_user()
        self.first = Fragment.objects.create(number=1, value="7F3A")
        self.second = Fragment.objects.create(number=2, value="b0b0")
        Fragment.objects.create(number=3, value="c4fe")

    def ask(self, command):
        return self.client.post(reverse("countdown_ask"), {"command": command}).json()

    def test_the_key_directory_lists_the_fragments(self):
        for command in ("ls key", "ls key/", "LS  ./key/"):
            with self.subTest(command=command):
                self.assertEqual(self.ask(command)["text"], "1.frag  2.frag  3.frag")

    def test_without_fragments_there_is_no_key_directory(self):
        Fragment.objects.all().delete()

        self.assertEqual(self.ask("ls key")["text"], "ls: key: нет такого каталога")
        self.assertEqual(self.ask("cat key/1.frag")["text"], "cat: key/1.frag: нет такого файла")

    def test_a_fragment_stays_damaged_until_its_copy_is_handed_in(self):
        self.client.force_login(self.person)

        damaged = self.ask("cat key/1.frag")["text"]
        self.assertIn("░░░░", damaged)
        self.assertIn("found <копия>", damaged)
        self.assertNotIn("7F3A", damaged)

        self.assertEqual(self.ask("found 7f3a")["text"], "копия верна. 1.frag восстановлен. сверено 1 из 3.")
        self.assertEqual(self.ask("cat ./key/1.frag")["text"], "1.frag: 7F3A")

    def test_a_copy_handed_in_tells_the_page_what_to_open(self):
        """Строку ключа и журнал страница перерисовывает сама, без перезагрузки."""
        self.client.force_login(self.person)

        answer = self.ask("found b0b0")

        self.assertEqual(answer["slot"], {"number": 2, "value": "b0b0"})
        self.assertEqual((answer["done"], answer["total"]), (1, 3))

    def test_a_copy_is_taken_as_the_clue_gave_it(self):
        """Подсказка может нести фрагмент с подписью — сдают и так, и одним значением."""
        self.client.force_login(self.person)

        self.ask("found FRAG[2/3]= B0 B0")

        self.assertTrue(Find.objects.filter(user=self.person, fragment=self.second).exists())

    def test_a_copy_counts_once(self):
        self.client.force_login(self.person)
        self.ask("found 7f3a")

        self.assertEqual(self.ask("found 7F3A")["text"], "1.frag уже восстановлен. сверено 1 из 3.")
        self.assertEqual(Find.objects.count(), 1)

    def test_found_alone_tells_what_one_has(self):
        self.client.force_login(self.person)
        self.assertEqual(self.ask("found")["text"], "сверено 0 из 3.")

        self.ask("found b0b0")

        self.assertEqual(self.ask("found")["text"], "сверено 1 из 3.  [2] b0b0")

    def test_a_wrong_copy_is_laughed_at(self):
        self.client.force_login(self.person)

        answer = self.ask("found zzzz")

        self.assertEqual(answer, {"text": "копия не подходит.", "laugh": True})
        self.assertFalse(Find.objects.exists())

    @patch("quest.keys.WRONG_PER_HOUR", 2)
    def test_guessing_closes_the_channel_even_for_a_right_copy(self):
        self.client.force_login(self.person)
        self.ask("found aaaa")
        self.ask("found bbbb")

        answer = self.ask("found 7f3a")

        self.assertTrue(answer["warn"])
        self.assertFalse(Find.objects.exists())

    def test_guests_cannot_hand_copies_in(self):
        answer = self.ask("found 7f3a")

        self.assertTrue(answer["warn"])
        self.assertFalse(Find.objects.exists())

    def test_someone_elses_copy_is_not_mine(self):
        Find.objects.create(user=make_user("other@t.local"), fragment=self.first)
        self.client.force_login(self.person)

        self.assertIn("░░░░", self.ask("cat key/1.frag")["text"])
        self.assertEqual(self.ask("found")["text"], "сверено 0 из 3.")

    def test_the_countdown_shows_the_damaged_key_and_only_ones_own_copies(self):
        Find.objects.create(user=self.person, fragment=self.second)
        Find.objects.create(user=make_user("other@t.local"), fragment=self.first)
        self.client.force_login(self.person)

        page = self.client.get(reverse("countdown")).content.decode()

        self.assertIn("ключ повреждён:", page)
        self.assertIn('<span class="cd-ok" data-slot="2">b0b0</span>', page)
        self.assertEqual(page.count('<span class="cd-redacted" data-slot'), 2)
        self.assertNotIn("7F3A", page)
        self.assertNotIn("c4fe", page)

    def test_a_guest_sees_the_key_all_damaged(self):
        page = self.client.get(reverse("countdown")).content.decode()

        self.assertEqual(page.count('<span class="cd-redacted" data-slot'), 3)

    def test_without_fragments_the_countdown_says_nothing_of_a_key(self):
        Fragment.objects.all().delete()

        self.assertNotContains(self.client.get(reverse("countdown")), "ключ повреждён")


@override_settings(SITE_THEME="redday")
class UnlockTests(QuestCase):
    """1 декабря: ключ целиком в консоли — награда и рамка, раз на человека (quest.keys.unlock)."""

    def setUp(self):
        super().setUp()
        self.person = make_user()
        for number, value in enumerate(("7F3A", "b0b0", "c4fe"), 1):
            Fragment.objects.create(number=number, value=value)

    def unlock(self, key):
        return self.client.post(reverse("countdown_ask"), {"command": f"unlock {key}".strip()}).json()

    def paid(self):
        return list(BalanceLog.objects.filter(wallet__user=self.person, reason=BalanceLog.Reason.QUEST).values_list("amount", flat=True))

    @override_settings(SITE_THEME="glitch")
    def test_before_zero_it_is_too_early(self):
        self.client.force_login(self.person)

        self.assertEqual(self.unlock("7f3a b0b0 c4fe")["text"], "рано. время ещё не вышло.")
        self.assertFalse(Unlock.objects.exists())

    def test_a_guest_is_asked_to_log_in(self):
        answer = self.unlock("7f3a b0b0 c4fe")

        self.assertTrue(answer["warn"])
        self.assertFalse(Unlock.objects.exists())

    def test_the_key_is_the_fragments_in_order_whatever_the_separators(self):
        self.client.force_login(self.person)

        answer = self.unlock("7f3a-B0B0 c4fe")

        self.assertTrue(answer["unlocked"])
        self.assertTrue(Unlock.objects.filter(user=self.person).exists())

    def test_a_sign_inside_a_fragment_does_not_lock_the_key_for_good(self):
        """Из админки такое значение не пропустит проверка, но база стерпит: знак чистится
        так же, как в набранном ключе."""
        Fragment.objects.filter(number=2).update(value="b0-b0")
        self.client.force_login(self.person)

        self.assertTrue(self.unlock("7f3a b0b0 c4fe").get("unlocked"))

    def test_the_wrong_order_does_not_open(self):
        self.client.force_login(self.person)

        answer = self.unlock("b0b0 7f3a c4fe")

        self.assertEqual(answer, {"text": "ключ не подошёл.", "laugh": True})
        self.assertFalse(Unlock.objects.exists())

    def test_the_key_pays_once_with_tokens_and_the_frame(self):
        frame = make_frame("Битый смайл")
        Broadcast.objects.create(frame=frame)
        self.client.force_login(self.person)

        answer = self.unlock("7f3ab0b0c4fe")
        again = self.unlock("7f3ab0b0c4fe")

        self.assertEqual(answer["text"], "ключ принят. сайт вернётся 2 декабря. тебе — 1500 токенов и рамка «Битый смайл».")
        self.assertEqual(again["text"], "ключ уже принят. сайт вернётся 2 декабря.")
        self.assertEqual(self.paid(), [1500])
        self.assertTrue(UserItem.objects.filter(user=self.person, item=frame).exists())

    @patch("quest.keys.FIRST_FINDERS", 2)
    def test_support_hears_of_the_first_few_who_opened_it(self):
        self.client.force_login(make_user("first@t.local", name="Анна", surname="Первая"))
        with patch("quest.keys.notify") as notify:
            with self.captureOnCommitCallbacks() as callbacks:
                self.unlock("7f3a b0b0 c4fe")
            notify.assert_not_called()  # только после коммита: откат не должен звонить
            for callback in callbacks:
                callback()
        runner_up = make_user("second@t.local")
        self.client.force_login(runner_up)
        with patch("quest.keys.notify") as second, self.captureOnCommitCallbacks(execute=True):
            self.unlock("7f3a b0b0 c4fe")
            self.unlock("7f3a b0b0 c4fe")  # повтор того же — не новость
            keys.reward(runner_up)  # и выдача по второму кругу, мимо консоли, — тоже
        self.client.force_login(make_user("third@t.local"))
        with patch("quest.keys.notify") as third, self.captureOnCommitCallbacks(execute=True):
            self.unlock("7f3a b0b0 c4fe")

        chat, template, context = notify.call_args.args
        self.assertEqual((chat, context["place"], context["person"].full_name), ("support", 1, "Первая Анна"))
        self.assertEqual(second.call_count, 1)
        third.assert_not_called()

    def test_the_support_message_names_the_finder(self):
        person = make_user("first@t.local", name="Анна", surname="Первая")

        text = render_to_string("quest/telegram_unlocked.html", {"person": person, "place": 1})

        self.assertIn("1-й: <b>Первая Анна</b>", text)

    def test_without_a_frame_there_are_only_tokens(self):
        self.client.force_login(self.person)

        answer = self.unlock("7f3a b0b0 c4fe")

        self.assertEqual(answer["text"], "ключ принят. сайт вернётся 2 декабря. тебе — 1500 токенов.")
        self.assertEqual(self.paid(), [1500])

    @patch("quest.keys.WRONG_KEYS_PER_HOUR", 2)
    def test_guessing_closes_the_channel_even_for_the_right_key(self):
        self.client.force_login(self.person)
        self.unlock("aaaa")
        self.unlock("bbbb")

        answer = self.unlock("7f3a b0b0 c4fe")

        self.assertTrue(answer["warn"])
        self.assertFalse(Unlock.objects.exists())

    def test_without_fragments_nothing_opens(self):
        """Даже «пустой» ключ из одних разделителей, совпавший бы с пустым ключом сайта."""
        Fragment.objects.all().delete()
        self.client.force_login(self.person)

        self.assertEqual(self.unlock("- - -")["text"], "ключ не подошёл.")
        self.assertFalse(Unlock.objects.exists())

    def test_only_the_key_holder_is_paid(self):
        other = make_user("other@t.local")
        self.client.force_login(self.person)
        self.unlock("7f3a b0b0 c4fe")

        rewards.sync(other)

        self.assertFalse(BalanceLog.objects.filter(wallet__user=other, reason=BalanceLog.Reason.QUEST).exists())

    def test_unlock_alone_shows_how_to_use_it(self):
        self.client.force_login(self.person)

        self.assertEqual(self.unlock("")["text"], "unlock <ключ>")


class ZeroPageTests(QuestCase):
    """Экран отсчёта после нуля: красный день, передача, ввод ключа."""

    def setUp(self):
        super().setUp()
        self.person = make_user()
        subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        playlist = Playlist.objects.create(title="Эфир", subject=subject, uploader=self.person, status=Playlist.Status.REJECTED)
        self.lecture = Lecture.objects.create(playlist=playlist, title="Передача", prefix="lectures/broadcast")
        Broadcast.objects.create(lecture=self.lecture)

    def page(self):
        return self.client.get(reverse("countdown")).content.decode()

    @override_settings(SITE_THEME="redday")
    def test_after_zero_the_broadcast_is_on_air(self):
        page = self.page()

        self.assertIn('class="theme-glitch dark redday"', page)
        self.assertIn("входящая передача", page)
        self.assertIn("lecturePlayer(", page)
        self.assertIn("', true)", page)  # консоль знает, что время вышло

    @override_settings(SITE_THEME="glitch")
    def test_before_zero_the_broadcast_stays_off_the_page(self):
        """Адрес записи раньше срока — это передача, найденная раньше срока."""
        response = self.client.get(reverse("countdown"))
        page = response.content.decode()

        self.assertNotIn("broadcast", response.context)
        self.assertNotIn("lecturePlayer(", page)
        self.assertNotIn("входящая передача", page)
        self.assertNotIn("сигнал потерян", page)
        self.assertIn("', false)", page)

    @override_settings(SITE_THEME="redday")
    def test_an_unbaked_broadcast_is_a_lost_signal(self):
        Lecture.objects.filter(pk=self.lecture.pk).update(prefix="")

        page = self.page()

        self.assertNotIn("lecturePlayer(", page)
        self.assertIn("сигнал потерян", page)

    @override_settings(SITE_THEME="redday")
    def test_one_who_opened_the_key_sees_it_accepted(self):
        Unlock.objects.create(user=self.person)
        self.client.force_login(self.person)

        self.assertIn(self.ACCEPTED.format("true"), self.page())

    @override_settings(SITE_THEME="redday")
    def test_others_see_the_line_only_after_their_own_key(self):
        Unlock.objects.create(user=make_user("other@t.local"))
        self.client.force_login(self.person)

        self.assertIn(self.ACCEPTED.format("false"), self.page())

    # Строка «ключ принят»: `{ open: … }` встречается и в меню аккаунта, так что ищем её целиком.
    ACCEPTED = 'x-data="{{ open: {} }}" x-show="open" @key-unlocked.window'


class NotFoundClueTests(QuestCase):
    """Строка-подсказка на странице 404 в скине."""

    def get(self):
        return Client(raise_request_exception=False).get("/wp-admin/setup.php")

    @override_settings(SITE_THEME="glitch")
    def test_the_404_page_carries_its_clue(self):
        Clue.objects.create(place=Clue.Place.NOTFOUND, text="в кэше осталось: frag[5/7]")

        self.assertContains(self.get(), "в кэше осталось: frag[5/7]", status_code=404)

    def test_without_the_skin_the_404_page_keeps_it(self):
        Clue.objects.create(place=Clue.Place.NOTFOUND, text="в кэше осталось: frag[5/7]")

        self.assertNotContains(self.get(), "frag[5/7]", status_code=404)

    @override_settings(SITE_THEME="glitch")
    def test_a_broken_store_does_not_break_the_404_page(self):
        with patch("quest.templatetags.quest_extras.clues", side_effect=RuntimeError("Redis лёг")), \
                self.assertLogs("quest.templatetags.quest_extras", "ERROR"):
            response = self.get()

        self.assertContains(response, "Такой страницы нет", status_code=404)
        self.assertNotContains(response, "skin-error-trace", status_code=404)


@override_settings(SITE_THEME="glitch")
class PageClueTests(QuestCase):
    """Журнал взлома и данные страницы: что попадает в разметку, а что — нет."""

    def setUp(self):
        super().setUp()
        self.person = make_user()
        self.client.force_login(self.person)

    def data(self, page, name):
        """Данные страницы для glitch.js — из-под занавески (quest_extras.veil)."""
        head = f'<script id="{name}" type="text/plain">'
        start = page.index(head) + len(head)
        raw = base64.b64decode(page[start:page.index("</script>", start)])
        return json.loads(bytes(byte ^ VEIL[i % len(VEIL)] for i, byte in enumerate(raw)).decode())

    def journal(self, page):
        return self.data(page, "glitch-log")

    def page(self):
        return self.client.get(reverse("material_list")).content.decode()

    def assertNowhere(self, text, page):
        """Ни в разметке, ни в данных страницы."""
        self.assertNotIn(text, page)
        self.assertNotIn(json.dumps(text)[1:-1], page)
        for name in ("glitch-log", "glitch-clues"):
            if f'id="{name}"' in page:
                self.assertNotIn(text, json.dumps(self.data(page, name), ensure_ascii=False))

    def test_clues_in_the_page_are_not_plain_text(self):
        """«Просмотр кода» и поиск по странице не выдают подсказок, минуя их места."""
        Clue.objects.create(place=Clue.Place.LOG, text="frag[4/7]=d00d")
        Clue.objects.create(place=Clue.Place.CARD, text="frag[3/7]=c4fe")

        page = self.page()

        for text in ("d00d", "c4fe", "frag["):
            with self.subTest(text=text):
                self.assertNotIn(text, page)
        self.assertIn("frag[4/7]=d00d", self.journal(page))
        self.assertEqual(self.data(page, "glitch-clues")["card"], ["frag[3/7]=c4fe"])

    def test_the_journal_names_only_its_reader(self):
        make_user("other@t.local", name="Пётр", surname="Петров")

        page = self.page()

        self.assertIn(f"Смирнов Илья → {reverse('material_list')}", self.journal(page))
        self.assertNowhere("Пётр", page)

    def test_the_journal_counts_only_published_books(self):
        Book.objects.create(title="Ландау", status=Moderated.Status.APPROVED)
        Book.objects.create(title="Черновик", status=Moderated.Status.PENDING)

        self.assertIn("копирование /library … 1 книга", self.journal(self.page()))

    def test_log_clues_are_spread_round_the_journal(self):
        """Подсказки — вразбивку и не в начале: подряд или вверху они бросались бы в глаза."""
        for text in ("фрагмент: 7f", "фрагмент: b0"):
            Clue.objects.create(place=Clue.Place.LOG, text=text)

        lines = self.journal(self.page())
        first, second = lines.index("фрагмент: 7f"), lines.index("фрагмент: b0")

        self.assertGreaterEqual(first, 6)
        self.assertGreaterEqual(second - first, 6)
        self.assertGreaterEqual(len(lines) - second, 6)

    def test_a_clue_comes_round_at_least_once_a_minute(self):
        """Журнал меняет строку раз в JOURNAL_STEP секунд — круг со всеми подсказками и строкой
        ключа не должен быть длиннее минуты, но и не короче половины: иначе мелькает."""
        for number in range(3):
            Clue.objects.create(place=Clue.Place.LOG, text=f"фрагмент {number}")
        Fragment.objects.create(number=1, value="7f3a")

        lines = self.journal(self.page())

        self.assertLessEqual(len(lines) * JOURNAL_STEP, ROUND)
        self.assertGreaterEqual(len(lines) * JOURNAL_STEP, ROUND / 2)

    def test_card_browser_and_denial_clues_ride_with_the_page(self):
        Clue.objects.create(place=Clue.Place.CARD, text="КЛЮЧ")
        Clue.objects.create(place=Clue.Place.DEVTOOLS, text="загляни в ls")
        Clue.objects.create(place=Clue.Place.DENIED, text="упрямым — frag[6/7]")

        self.assertEqual(self.data(self.page(), "glitch-clues"),
                         {"card": ["КЛЮЧ"], "devtools": ["загляни в ls"], "denied": ["упрямым — frag[6/7]"]})

    def test_the_journal_tells_how_much_of_the_key_one_has(self):
        first = Fragment.objects.create(number=1, value="7f3a")
        second = Fragment.objects.create(number=2, value="b0b0")
        Find.objects.create(user=self.person, fragment=first)
        Find.objects.create(user=make_user("other@t.local"), fragment=second)

        self.assertIn("сверка ключа … 1/2", self.journal(self.page()))

    def test_without_fragments_the_journal_keeps_quiet_about_the_key(self):
        self.assertNotIn("сверка ключа", " ".join(self.journal(self.page())))

    def test_console_answers_never_reach_the_page(self):
        """Ответ на команду отдаёт только quest.views.ask — в разметке его не найти."""
        Clue.objects.create(place=Clue.Place.COMMAND, command="cat .secret/key", text="ответ-секрет")

        for name in ("material_list", "countdown"):
            with self.subTest(page=name):
                page = self.client.get(reverse(name)).content.decode()
                self.assertNowhere("ответ-секрет", page)
                self.assertNowhere(".secret/key", page)

    @override_settings(SITE_THEME="default")
    def test_without_the_skin_there_is_no_journal(self):
        Clue.objects.create(place=Clue.Place.LOG, text="фрагмент")

        page = self.page()

        self.assertNotIn("glitch-log", page)
        self.assertNotIn("glitch-clues", page)
        self.assertNowhere("фрагмент", page)


@override_settings(SITE_THEME="party")
class PartyTests(QuestCase):
    """2 декабря — праздник после взлома: праздничная страница, ссылка в шапке, тема снова своя."""

    def page(self):
        return self.client.get(reverse("party"))

    def test_the_root_leads_everyone_to_the_party(self):
        self.assertRedirects(self.client.get("/"), reverse("party"))
        self.client.force_login(make_user())
        self.assertRedirects(self.client.get("/"), reverse("party"))

    def test_a_guest_sees_it_and_search_engines_do_not(self):
        response = self.page()

        self.assertContains(response, "Сайт свободен!")
        self.assertContains(response, '<meta name="robots" content="noindex">', html=True)
        self.assertNotContains(response, "Мои рамки")

    def test_the_party_is_only_on_its_day(self):
        for theme in ("default", "glitch", "redday", "halloween"):
            with self.subTest(theme=theme), self.settings(SITE_THEME=theme):
                self.assertEqual(self.page().status_code, 404)

    def test_on_the_party_day_the_countdown_is_gone(self):
        self.assertEqual(self.client.get(reverse("countdown")).status_code, 404)
        self.assertEqual(self.client.post(reverse("countdown_ask"), {"command": "unlock 7f3a"}).status_code, 404)

    def test_the_numbers_are_real(self):
        first, second = Fragment.objects.create(number=1, value="7f3a"), Fragment.objects.create(number=2, value="b0b0")
        people = [make_user(f"p{n}@t.local") for n in range(3)]
        for person in people[:2]:
            Unlock.objects.create(user=person)
        for person, fragment in ((people[0], first), (people[0], second), (people[2], first)):
            Find.objects.create(user=person, fragment=fragment)
        Tally.objects.create(name=Tally.COMMANDS, count=12345)

        page = self.page().content.decode()

        self.assertIn("<li><b>2</b>нашли ключ</li>", page)
        self.assertIn("<li><b>3</b>фрагментов сдано</li>", page)
        self.assertIn("<li><b>12 345</b>команд в консоли</li>", page)
        self.assertIn("<li><b>13</b>дней взлома</li>", page)  # 18 ноя – 30 ноя

    def test_those_who_found_the_key_are_not_named(self):
        Unlock.objects.create(user=make_user("found@t.local", name="Пётр", surname="Петров"))

        self.assertNotContains(self.page(), "Петров")

    def test_the_frame_card_is_there_only_with_a_frame(self):
        self.assertNotContains(self.page(), "Рамка «")

        Broadcast.objects.create(frame=make_frame("Битый смайл"))
        self.client.force_login(make_user())

        response = self.page()
        self.assertContains(response, "Рамка «Битый смайл»")
        self.assertContains(response, "Мои рамки")

    def test_ones_own_hack_shows_ones_own_key(self):
        """«Твой взлом»: свой ключ ячейками и итог — чужие находки в нём не видны."""
        first, second = Fragment.objects.create(number=1, value="7f3a"), Fragment.objects.create(number=2, value="b0b0")
        Fragment.objects.create(number=3, value="c4fe")
        person = make_user()
        Find.objects.create(user=person, fragment=first)
        Find.objects.create(user=person, fragment=second)
        Find.objects.create(user=make_user("other@t.local"), fragment=Fragment.objects.get(number=3))
        self.client.force_login(person)

        page = self.page().content.decode()

        # --key-len — вся строка в знаках, с пробелом между ячейками: 4+1+4+1+4.
        self.assertIn('<p class="party-key" style="--key-len: 14" aria-hidden="true">'
                      '<span>7f3a</span> <span>b0b0</span> <span class="party-key-lost">░░░░</span></p>', page)
        self.assertIn("Сдано фрагментов: 2 из 3. Ключ остался у взломщиков.", page)

    def test_one_who_opened_the_key_is_told_the_reward(self):
        Fragment.objects.create(number=1, value="7f3a")
        person = make_user()
        Unlock.objects.create(user=person)
        self.client.force_login(person)

        self.assertContains(self.page(), "Сдано фрагментов: 0 из 1. Ключ вскрыт — тебе 1500 токенов.")

    def test_a_guest_is_asked_to_log_in_for_the_results(self):
        Fragment.objects.create(number=1, value="7f3a")
        Fragment.objects.create(number=2, value="b0b0")

        page = self.page().content.decode()

        self.assertIn("Чтобы узнать свои результаты, войди.", page)
        self.assertIn(f'href="{reverse("login")}?next={reverse("party")}"', page)
        self.assertIn('<p class="party-key" style="--key-len: 9" aria-hidden="true">'
                      '<span class="party-key-lost">░░░░</span> <span class="party-key-lost">░░░░</span></p>', page)
        self.assertNotIn("Сдано фрагментов", page)

    def test_without_a_quest_there_is_no_result_card(self):
        self.assertNotContains(self.page(), "Твой взлом")
        self.client.force_login(make_user())
        self.assertNotContains(self.page(), "Твой взлом")

    def test_the_header_links_to_the_party_except_on_it(self):
        self.client.force_login(make_user())

        self.assertContains(self.client.get(reverse("material_list")), f'href="{reverse("party")}" class="party-pill"')
        self.assertNotContains(self.page(), 'class="party-pill"')

    def test_the_theme_is_ones_own_again(self):
        """Тёмную тему праздник не навязывает, и переключатель снова работает."""
        self.client.force_login(make_user())

        page = self.client.get(reverse("material_list")).content.decode()

        self.assertIn('<html lang="ru" class="theme-party">', page)
        self.assertIn("localStorage.setItem('color-scheme'", page)
        self.assertNotIn("lockedSwitch", page)
        self.assertNotIn("glitch.js", page)
