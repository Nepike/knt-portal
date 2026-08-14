import random
import tempfile
from io import StringIO
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from economy.models import BalanceLog
from economy.services import NotEnoughFunds, credit, wallet_of
from users.models import User

from . import palette, rules
from .models import Board, Pixel, Placement, ProtectedArea, WallProfile
from .services import (
    MARK_EVERY,
    NoCharges,
    WallError,
    ban,
    erase,
    fill,
    history,
    journal,
    open_board,
    paint,
    profile_of,
    protect,
    reroll,
    rollback,
    snapshot,
    status,
    version,
)

MANUAL = BalanceLog.Reason.MANUAL


def make_user(email="u@t.local"):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


def make_board(**extra):
    return Board.objects.create(title="Стена", width=8, height=4, **extra)


def make_moderator(email="mod@t.local"):
    user = make_user(email)
    user.user_permissions.add(Permission.objects.get(codename="moderate_wall"))
    return User.objects.get(pk=user.pk)  # права кешируются на объекте, берём свежий


def put(user, board, x, y, color=None, free=False):
    """Мазок в тестах: цвет по умолчанию — закреплённый за человеком. Большинству
    проверок всё равно, каким именно красили, и так они короче."""
    return paint(user, board, x, y, profile_of(user).color if color is None else color, free=free)


def spread(board, n):
    """Клетка номер n подряд, с переходом на следующий ряд: зарядов больше, чем
    ширина у тестовой доски."""
    return n % board.width, n // board.width


def set_color(user, name):
    """Ставим человеку конкретный цвет — со случайным в тестах нечего сверять."""
    code = next(c.code for c in palette.PICKABLE if c.name == name)
    profile_of(user)  # профиль заводится лениво, до него обновлять нечего
    WallProfile.objects.filter(user=user).update(color=code)
    return code


class PaletteTests(TestCase):
    def test_code_is_the_position_in_the_list(self):
        for index, color in enumerate(palette.PALETTE):
            self.assertEqual(color.code, index)

    def test_the_palette_is_a_grid_plus_a_neutral_row(self):
        self.assertEqual(
            len(palette.PICKABLE), len(palette.TONES) * len(palette.HUES) + len(palette.NEUTRALS),
        )
        self.assertEqual(palette.get(palette.EMPTY).name, "бетон")
        self.assertEqual([palette.PALETTE[-1].name, palette.PALETTE[-len(palette.NEUTRALS)].name],
                         ["мел", "тушь"])

    def test_every_hue_has_the_whole_ladder(self):
        """Рисуют лесенкой из соседних светлот одного тона — она должна быть целой."""
        for hue in palette.HUES:
            ladder = [color for color in palette.PICKABLE if color.name.endswith(f" {hue}")]
            self.assertEqual([color.name for color in ladder],
                             [f"{tone} {hue}" for tone in palette.TONES])

    def test_names_are_unique(self):
        names = [c.name for c in palette.PALETTE]
        self.assertEqual(len(names), len(set(names)))

    def test_roll_never_returns_the_empty_cell(self):
        self.assertNotIn(palette.EMPTY, {palette.roll() for _ in range(200)})

    def test_roll_never_repeats_the_excluded_color(self):
        self.assertNotIn(7, {palette.roll(exclude=7) for _ in range(200)})

    def test_roll_reaches_every_color(self):
        random.seed(20260812)
        self.assertEqual(len({palette.roll() for _ in range(3000)}), len(palette.PICKABLE))


class BoardTests(TestCase):
    def test_only_one_board_can_be_current(self):
        make_board()
        with self.assertRaises(IntegrityError):
            make_board()

    def test_an_archived_board_does_not_block_a_new_one(self):
        make_board(is_active=False, closed=timezone.now())
        make_board()  # не должно упасть

    def test_bounds(self):
        board = make_board()
        self.assertTrue(board.holds(7, 3))
        self.assertFalse(board.holds(8, 3))
        self.assertFalse(board.holds(-1, 0))


class ChargeTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.board = make_board()

    def rewind(self, minutes):
        """Отматываем отсчёт назад — это то же самое, что подождать."""
        profile = profile_of(self.user)
        WallProfile.objects.filter(user=self.user).update(
            charged_at=profile.charged_at - timedelta(minutes=minutes),
        )

    def test_a_newcomer_starts_with_a_full_stack(self):
        self.assertEqual(profile_of(self.user).charges, rules.MAX_CHARGES)

    def test_painting_spends_one_charge(self):
        put(self.user, self.board, 0, 0)
        self.assertEqual(profile_of(self.user).charges, rules.MAX_CHARGES - 1)

    def test_running_out_stops_the_brush(self):
        for n in range(rules.MAX_CHARGES):
            put(self.user, self.board, *spread(self.board, n))
        with self.assertRaises(NoCharges):
            put(self.user, self.board, 7, 3)
        self.assertEqual(Placement.objects.count(), rules.MAX_CHARGES)

    def test_charges_come_back_with_time(self):
        for n in range(rules.MAX_CHARGES):
            put(self.user, self.board, *spread(self.board, n))
        self.rewind(minutes=7)  # два интервала по три минуты и остаток
        self.assertEqual(status(profile_of(self.user))[0], 2)

    def test_the_stack_does_not_overflow(self):
        put(self.user, self.board, 0, 0)
        self.rewind(minutes=600)
        self.assertEqual(status(profile_of(self.user))[0], rules.MAX_CHARGES)

    def test_leftover_time_is_not_burned(self):
        """Накопил полтора интервала, потратил заряд — половина должна остаться."""
        for n in range(rules.MAX_CHARGES):
            put(self.user, self.board, *spread(self.board, n))
        self.rewind(minutes=4.5)
        put(self.user, self.board, 7, 3)
        profile = profile_of(self.user)
        self.assertEqual(profile.charges, 0)
        left = profile.charged_at + rules.CHARGE_INTERVAL - timezone.now()
        self.assertLess(left, timedelta(minutes=1.6))

    def test_a_full_stack_reports_no_timer(self):
        self.assertEqual(status(profile_of(self.user))[1], None)


class PaintTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.board = make_board()
        self.color = set_color(self.user, "основной синий")

    def test_painting_writes_the_pixel_and_the_journal(self):
        put(self.user, self.board, 3, 2)
        pixel = Pixel.objects.get(board=self.board, x=3, y=2)
        self.assertEqual((pixel.color, pixel.user), (self.color, self.user))
        self.assertEqual(Placement.objects.count(), 1)

    def test_painting_over_a_stranger_replaces_the_owner(self):
        other = make_user("o@t.local")
        theirs = set_color(other, "основной красный")
        put(other, self.board, 1, 1)
        put(self.user, self.board, 1, 1)

        pixel = Pixel.objects.get(board=self.board, x=1, y=1)
        self.assertEqual((pixel.color, pixel.user), (self.color, self.user))
        self.assertEqual([p.color for p in history(self.board, 1, 1)], [self.color, theirs])

    def test_outside_the_board_is_refused(self):
        with self.assertRaises(WallError):
            put(self.user, self.board, 8, 0)
        self.assertEqual(Placement.objects.count(), 0)

    def test_a_closed_board_is_refused(self):
        board = make_board(is_active=False)
        Board.objects.filter(pk=self.board.pk).update(is_active=False)
        with self.assertRaises(WallError):
            put(self.user, board, 0, 0)

    def test_a_banned_person_is_refused(self):
        WallProfile.objects.filter(user=self.user).update(
            banned_until=timezone.now() + timedelta(days=1),
        )
        with self.assertRaises(WallError):
            put(self.user, self.board, 0, 0)

    def test_an_expired_ban_lets_the_person_back(self):
        WallProfile.objects.filter(user=self.user).update(
            banned_until=timezone.now() - timedelta(days=1),
        )
        put(self.user, self.board, 0, 0)
        self.assertEqual(Placement.objects.count(), 1)


class EraseTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.board = make_board()
        set_color(self.user, "основной синий")

    def test_erasing_your_own_pixel_empties_the_cell(self):
        put(self.user, self.board, 2, 2)
        erase(self.user, self.board, 2, 2)
        self.assertEqual(Pixel.objects.get(board=self.board, x=2, y=2).color, palette.EMPTY)

    def test_erasing_is_free(self):
        put(self.user, self.board, 2, 2)
        left = profile_of(self.user).charges
        erase(self.user, self.board, 2, 2)
        self.assertEqual(profile_of(self.user).charges, left)

    def test_a_stranger_pixel_cannot_be_erased(self):
        other = make_user("o@t.local")
        put(other, self.board, 2, 2)
        with self.assertRaises(WallError):
            erase(self.user, self.board, 2, 2)
        self.assertNotEqual(Pixel.objects.get(board=self.board, x=2, y=2).color, palette.EMPTY)

    def test_an_empty_cell_cannot_be_erased(self):
        with self.assertRaises(WallError):
            erase(self.user, self.board, 2, 2)

    def test_the_eraser_stays_in_the_history(self):
        put(self.user, self.board, 2, 2)
        erase(self.user, self.board, 2, 2)
        last = history(self.board, 2, 2)[0]
        self.assertEqual((last.color, last.user), (palette.EMPTY, self.user))

    def test_erasing_the_cell_you_painted_over_is_allowed(self):
        """Закрасил чужое — пиксель стал твой, а свой стирать можно."""
        other = make_user("o@t.local")
        put(other, self.board, 2, 2)
        put(self.user, self.board, 2, 2)
        erase(self.user, self.board, 2, 2)
        self.assertEqual(Pixel.objects.get(board=self.board, x=2, y=2).color, palette.EMPTY)


class SnapshotTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.board = make_board()
        self.color = set_color(self.user, "основной синий")

    def test_snapshot_is_one_byte_per_cell(self):
        self.assertEqual(len(snapshot(self.board)), 8 * 4)

    def test_the_cell_lands_in_its_place(self):
        put(self.user, self.board, 3, 2)
        self.assertEqual(snapshot(self.board)[2 * 8 + 3], self.color)

    def test_an_erased_cell_goes_back_to_zero(self):
        put(self.user, self.board, 3, 2)
        erase(self.user, self.board, 3, 2)
        self.assertEqual(set(snapshot(self.board)), {palette.EMPTY})

    def test_version_follows_the_last_event(self):
        self.assertEqual(version(self.board), 0)
        last = put(self.user, self.board, 0, 0)
        self.assertEqual(version(self.board), last.pk)


class OwnColorTests(TestCase):
    """Закрепление цвета за аккаунтом сейчас выключено, но код на месте — проверяем оба
    положения переключателя, чтобы возврат не оказался сюрпризом."""

    def setUp(self):
        self.user = make_user()
        self.board = make_board()
        self.mine = set_color(self.user, "основной синий")
        self.other = next(c.code for c in palette.PICKABLE if c.name == "основной зелёный")

    def test_an_open_palette_honours_the_chosen_color(self):
        put(self.user, self.board, 0, 0, self.other)
        self.assertEqual(Pixel.objects.get(x=0, y=0).color, self.other)

    def test_a_locked_palette_puts_the_assigned_color_instead(self):
        with mock.patch.object(rules, "OWN_COLOR_ONLY", True):
            put(self.user, self.board, 0, 0, self.other)
        self.assertEqual(Pixel.objects.get(x=0, y=0).color, self.mine)

    def test_there_is_nothing_to_reroll_while_the_palette_is_open(self):
        self.client.force_login(self.user)
        credit(self.user, rules.REROLL_PRICE, MANUAL)
        self.assertEqual(self.client.post(reverse("wall_reroll")).status_code, 404)


class NewBoardTests(TestCase):
    def setUp(self):
        self.board = make_board()
        self.admin = make_user("a@t.local")
        User.objects.filter(pk=self.admin.pk).update(is_staff=True)
        self.admin.refresh_from_db()

    def test_a_new_board_sends_the_old_one_to_the_archive(self):
        put(make_user("p@t.local"), self.board, 1, 1)
        fresh = open_board(self.admin, "Стена, осень")
        self.board.refresh_from_db()
        self.assertFalse(self.board.is_active)
        self.assertIsNotNone(self.board.closed)
        self.assertEqual(Board.current(), fresh)
        self.assertEqual((fresh.width, fresh.height), (self.board.width, self.board.height))

    def test_the_archived_board_keeps_its_art_while_the_new_one_starts_empty(self):
        put(make_user("p@t.local"), self.board, 1, 1)
        open_board(self.admin, "Стена, осень")
        self.assertEqual(Pixel.objects.filter(board=self.board).count(), 1)
        self.assertEqual(set(snapshot(Board.current())), {palette.EMPTY})
        self.assertEqual(journal(Board.current())[2], b"")

    def test_a_moderator_who_is_not_staff_cannot_open_one(self):
        with self.assertRaises(WallError):
            open_board(make_moderator("m2@t.local"), "Своя доска")
        self.assertTrue(Board.current().is_active)

    def test_a_board_needs_a_title(self):
        with self.assertRaises(WallError):
            open_board(self.admin, "   ")

    def test_through_the_view(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(reverse("wall_board_new"), {"title": "Весна"}).status_code, 200)
        self.assertEqual(Board.current().title, "Весна")

    def test_an_ordinary_person_is_refused_by_the_view(self):
        self.client.force_login(make_user("o@t.local"))
        self.assertEqual(self.client.post(reverse("wall_board_new"), {"title": "Моя"}).status_code, 400)
        self.assertEqual(Board.objects.count(), 1)


class JournalTests(TestCase):
    """Журнал для таймлапса. Держится на одном: события, наложенные подряд на пустое
    полотно, обязаны дать ровно ту доску, которую отдаёт снимок."""

    def setUp(self):
        self.user = make_user()
        self.board = make_board()
        self.color = set_color(self.user, "основной синий")

    def replay(self, events):
        cells = bytearray(self.board.width * self.board.height)
        for at in range(0, len(events), 3):
            x, y, color = events[at:at + 3]
            cells[y * self.board.width + x] = color
        return bytes(cells)

    def test_three_bytes_per_event_in_order(self):
        put(self.user, self.board, 3, 2)
        put(self.user, self.board, 0, 0)
        _, _, events = journal(self.board)
        self.assertEqual(events, bytes((3, 2, self.color, 0, 0, self.color)))

    def test_replaying_the_journal_gives_the_snapshot(self):
        mod = make_moderator("m@t.local")
        for x, y in ((1, 1), (2, 1), (3, 3)):
            put(self.user, self.board, x, y)
        erase(self.user, self.board, 2, 1)
        fill(mod, self.board, (4, 0, 6, 1), set_color(mod, "тушь"))
        rollback(mod, self.board, (4, 0, 6, 1), timezone.now() - timedelta(hours=1))
        _, _, events = journal(self.board)
        self.assertEqual(self.replay(events), snapshot(self.board))

    def test_the_first_mark_is_zero_and_they_come_every_step(self):
        for x in range(3):
            put(self.user, self.board, x, 0)
        start, marks, _ = journal(self.board)
        self.assertEqual(marks, [0])  # событий меньше шага — отметка одна
        self.assertEqual(start, Placement.objects.earliest("id").created)

    def test_an_untouched_board_has_no_journal(self):
        start, marks, events = journal(self.board)
        self.assertIsNone(start)
        self.assertEqual((marks, events), ([], b""))


class RerollTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.was = set_color(self.user, "основной синий")

    def test_reroll_changes_the_color_and_takes_the_money(self):
        credit(self.user, rules.REROLL_PRICE, MANUAL)
        now = reroll(self.user)
        self.assertNotEqual(now, self.was)
        self.assertEqual(wallet_of(self.user).balance, 0)
        self.assertEqual(profile_of(self.user).rerolls, 1)

    def test_without_money_the_color_stays(self):
        credit(self.user, rules.REROLL_PRICE - 1, MANUAL)
        with self.assertRaises(NotEnoughFunds):
            reroll(self.user)
        self.assertEqual(profile_of(self.user).color, self.was)
        self.assertEqual(wallet_of(self.user).balance, rules.REROLL_PRICE - 1)

    def test_the_journal_remembers_the_color_you_left(self):
        credit(self.user, rules.REROLL_PRICE, MANUAL)
        reroll(self.user)
        self.assertIn("основной синий", BalanceLog.objects.first().note)


class ArtistTests(TestCase):
    """Режим художника: модератор кладёт любой цвет и не тратит заряды."""

    def setUp(self):
        self.mod = make_moderator()
        self.board = make_board()
        set_color(self.mod, "основной синий")
        self.gold = next(c.code for c in palette.PICKABLE if c.name == "основной янтарный")

    def test_moderator_paints_without_spending_a_charge(self):
        put(self.mod, self.board, 1, 1, self.gold, free=True)
        self.assertEqual(Pixel.objects.get(x=1, y=1).color, self.gold)
        self.assertEqual(profile_of(self.mod).charges, rules.MAX_CHARGES)

    def test_a_moderator_who_does_not_ask_for_free_pays_like_everyone(self):
        put(self.mod, self.board, 1, 1, self.gold)
        self.assertEqual(profile_of(self.mod).charges, rules.MAX_CHARGES - 1)

    def test_an_ordinary_person_cannot_paint_for_free(self):
        stranger = make_user("o@t.local")
        with self.assertRaises(WallError):
            put(stranger, self.board, 1, 1, self.gold, free=True)
        self.assertEqual(Placement.objects.count(), 0)

    def test_a_color_outside_the_palette_is_refused(self):
        for bad in (0, 999, -1):
            with self.assertRaises(WallError):
                put(self.mod, self.board, 1, 1, bad)

    def test_a_moderator_may_erase_a_stranger_pixel(self):
        other = make_user("o@t.local")
        put(other, self.board, 2, 2)
        erase(self.mod, self.board, 2, 2)
        self.assertEqual(Pixel.objects.get(x=2, y=2).color, palette.EMPTY)


class AreaTests(TestCase):
    def setUp(self):
        self.mod = make_moderator()
        self.board = make_board()
        self.color = set_color(self.mod, "основной синий")

    def test_fill_paints_the_whole_rectangle(self):
        fill(self.mod, self.board, (1, 1, 3, 2), self.color)
        painted = Pixel.objects.exclude(color=palette.EMPTY).values_list("x", "y")
        self.assertEqual(sorted(painted), [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2)])

    def test_corners_may_come_in_any_order(self):
        fill(self.mod, self.board, (3, 2, 1, 1), self.color)
        self.assertEqual(Pixel.objects.exclude(color=palette.EMPTY).count(), 6)

    def test_fill_writes_the_journal_once_per_cell(self):
        fill(self.mod, self.board, (0, 0, 1, 1), self.color)
        self.assertEqual(Placement.objects.count(), 4)

    def test_cells_that_already_match_are_left_alone(self):
        fill(self.mod, self.board, (0, 0, 1, 1), self.color)
        fill(self.mod, self.board, (0, 0, 1, 1), self.color)
        self.assertEqual(Placement.objects.count(), 4)  # второй заход ничего не менял

    def test_clearing_is_the_same_fill_with_an_empty_color(self):
        fill(self.mod, self.board, (0, 0, 2, 2), self.color)
        fill(self.mod, self.board, (0, 0, 2, 2), palette.EMPTY)
        self.assertEqual(set(snapshot(self.board)), {palette.EMPTY})

    def test_an_area_beyond_the_board_is_refused(self):
        with self.assertRaises(WallError):
            fill(self.mod, self.board, (0, 0, 99, 99), self.color)

    def test_too_large_an_area_is_refused(self):
        board = Board.objects.create(title="Большая", width=200, height=200, is_active=False)
        with self.assertRaises(WallError):
            fill(self.mod, board, (0, 0, 199, 199), self.color)

    def test_an_ordinary_person_has_no_fill(self):
        with self.assertRaises(WallError):
            fill(make_user("o@t.local"), self.board, (0, 0, 1, 1), self.color)


class RollbackTests(TestCase):
    def setUp(self):
        self.mod = make_moderator()
        self.board = make_board()
        self.author = make_user("a@t.local")
        self.was = set_color(self.author, "основной зелёный")
        set_color(self.mod, "основной синий")

    def test_rollback_restores_what_was_under_the_paint(self):
        put(self.author, self.board, 1, 1)
        moment = timezone.now()
        Placement.objects.update(created=moment - timedelta(hours=2))
        Pixel.objects.update(placed=moment - timedelta(hours=2))

        grief = make_user("g@t.local")
        set_color(grief, "основной красный")
        put(grief, self.board, 1, 1)
        put(grief, self.board, 2, 2)

        rollback(self.mod, self.board, (0, 0, 3, 3), moment - timedelta(hours=1))
        self.assertEqual(Pixel.objects.get(x=1, y=1).color, self.was)  # чужой мазок снят
        self.assertEqual(Pixel.objects.get(x=2, y=2).color, palette.EMPTY)  # там ничего и не было

    def test_rollback_leaves_a_trace_in_the_journal(self):
        put(self.author, self.board, 1, 1)
        rollback(self.mod, self.board, (0, 0, 2, 2), timezone.now() - timedelta(hours=1))
        last = history(self.board, 1, 1)[0]
        self.assertEqual((last.color, last.user), (palette.EMPTY, self.mod))

    def test_an_ordinary_person_has_no_rollback(self):
        with self.assertRaises(WallError):
            rollback(make_user("o@t.local"), self.board, (0, 0, 1, 1), timezone.now())


class ProtectedAreaTests(TestCase):
    def setUp(self):
        self.mod = make_moderator()
        self.board = make_board()
        self.person = make_user("p@t.local")
        set_color(self.person, "основной синий")
        set_color(self.mod, "основной красный")
        protect(self.mod, self.board, (1, 1, 3, 3), note="герб")

    def test_a_frozen_cell_is_closed_for_everyone_else(self):
        with self.assertRaises(WallError):
            put(self.person, self.board, 2, 2)
        self.assertEqual(profile_of(self.person).charges, rules.MAX_CHARGES)  # заряд не сгорел

    def test_outside_the_frame_the_board_still_works(self):
        put(self.person, self.board, 0, 0)
        self.assertEqual(Placement.objects.count(), 1)

    def test_the_moderator_paints_through_the_freeze(self):
        put(self.mod, self.board, 2, 2)
        self.assertEqual(Placement.objects.count(), 1)

    def test_unfreezing_opens_it_back(self):
        ProtectedArea.objects.all().delete()
        put(self.person, self.board, 2, 2)
        self.assertEqual(Placement.objects.count(), 1)


class BanTests(TestCase):
    def setUp(self):
        self.mod = make_moderator()
        self.board = make_board()
        self.grief = make_user("g@t.local")
        set_color(self.grief, "основной синий")

    def test_ban_closes_the_board_but_not_the_site(self):
        ban(self.mod, self.grief, 7)
        with self.assertRaises(WallError):
            put(self.grief, self.board, 1, 1)
        self.assertTrue(User.objects.get(pk=self.grief.pk).is_active)

    def test_zero_days_lifts_the_ban(self):
        ban(self.mod, self.grief, 7)
        ban(self.mod, self.grief, 0)
        put(self.grief, self.board, 1, 1)
        self.assertEqual(Placement.objects.count(), 1)

    def test_an_ordinary_person_cannot_ban(self):
        with self.assertRaises(WallError):
            ban(make_user("o@t.local"), self.grief, 7)


class BroadcastTests(TestCase):
    def test_the_pixel_is_announced_only_after_the_commit(self):
        user = make_user()
        board = make_board()
        with mock.patch("wall.services.notify_pixel") as notify:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                placement = put(user, board, 1, 1)
                notify.assert_not_called()  # транзакция ещё не закрыта
            self.assertEqual(len(callbacks), 1)
            notify.assert_called_once_with(placement)

    def test_a_refused_pixel_is_not_announced(self):
        user = make_user()
        board = make_board()
        with mock.patch("wall.services.notify_pixel") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(WallError):
                    put(user, board, 99, 99)
            notify.assert_not_called()


class DrawTests(TestCase):
    """Перенос картинки на доску: подбор цвета и сама команда."""

    def setUp(self):
        self.mod = make_moderator()
        self.board = make_board()

    def picture(self, size=(8, 4), color=(255, 0, 0, 255)):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "art.png"
        Image.new("RGBA", size, color).save(path)
        return str(path)

    def draw(self, path, **options):
        call_command("wall_draw", path, user=self.mod.email, stdout=StringIO(), **options)

    def test_every_palette_color_finds_itself(self):
        for color in palette.PALETTE:
            self.assertEqual(palette.nearest(*palette.rgb(color.hex)), color.code)

    def test_a_grey_like_the_board_is_left_empty(self):
        """Фон картинки честнее не закрашивать: сквозь него виден сам бетон."""
        self.assertEqual(palette.nearest(0x93, 0x97, 0x9B), palette.EMPTY)

    def test_a_muted_color_does_not_jump_to_the_opposite_hue(self):
        """Ни пыльной розы, ни шалфея в палитре нет, попасть точно не во что. Но уйти
        приглушённый цвет обязан к соседям по кругу: без штрафа за тон он выбирал наугад."""
        rose = palette.get(palette.nearest(0xC0, 0x8A, 0x8A)).name
        sage = palette.get(palette.nearest(0x8F, 0xA9, 0x8F)).name
        self.assertTrue(rose.endswith(("красный", "розовый", "пурпурный")), rose)
        self.assertTrue(sage.endswith(("зелёный", "изумрудный", "янтарный")), sage)

    def test_a_warm_grey_lands_on_the_neutral_row(self):
        """Ради этого ряда он и заведён: контур рисунка — не бирюзовый и не оранжевый."""
        outline = palette.get(palette.nearest(0x6B, 0x5A, 0x4E))  # тёмно-коричневый контур
        self.assertIn(outline.name, {name for _, name in palette.NEUTRALS})

    def test_a_red_picture_becomes_a_red_board(self):
        self.draw(self.picture(), apply=True)
        colors = set(Pixel.objects.values_list("color", flat=True))
        self.assertEqual(len(colors), 1)
        self.assertIn("красный", palette.get(colors.pop()).name)
        self.assertEqual(Pixel.objects.count(), 8 * 4)

    def test_a_small_picture_keeps_its_proportions_and_lands_in_the_middle(self):
        self.draw(self.picture(size=(2, 2)), width=2, apply=True)
        self.assertEqual(
            sorted((p.x, p.y) for p in Pixel.objects.all()), [(3, 1), (3, 2), (4, 1), (4, 2)],
        )

    def test_without_apply_the_board_is_left_alone(self):
        self.draw(self.picture())
        self.assertEqual(Pixel.objects.count(), 0)

    def test_cells_are_written_in_layers_by_color(self):
        """Порядок записи — это порядок таймлапса: построчно картинка выезжала бы
        сканером сверху вниз, слоями она проступает целиком."""
        path = self.picture(size=(4, 2))
        image = Image.open(path)
        for row in range(2):
            image.putpixel((3, row), (0, 0, 255, 255))
        image.save(path)
        self.draw(path, width=4, x=0, y=0, apply=True)
        written = list(Placement.objects.order_by("id").values_list("color", flat=True))
        self.assertEqual(len(written), 8)
        self.assertEqual(len(set(written[:6])), 1)  # сначала слой покрупнее, целиком
        self.assertEqual(len(set(written[6:])), 1)

    def test_a_fully_transparent_picture_changes_nothing(self):
        self.draw(self.picture(size=(2, 1), color=(255, 0, 0, 0)), width=2, x=0, y=0, apply=True)
        self.assertEqual(Pixel.objects.count(), 0)
        self.assertEqual(Placement.objects.count(), 0)

    def test_transparent_pixels_are_not_painted(self):
        path = self.picture(size=(2, 1))
        image = Image.open(path)
        image.putpixel((0, 0), (0, 0, 0, 0))
        image.save(path)
        self.draw(path, width=2, x=0, y=0, apply=True)
        self.assertEqual([(p.x, p.y) for p in Pixel.objects.all()], [(1, 0)])

    def test_an_ordinary_person_cannot_stamp(self):
        with self.assertRaises(CommandError):
            call_command("wall_draw", self.picture(), user=make_user("o@t.local").email,
                         apply=True, stdout=StringIO())
        self.assertEqual(Pixel.objects.count(), 0)

    def test_a_picture_that_does_not_fit_is_refused(self):
        with self.assertRaises(CommandError):
            self.draw(self.picture(), x=7, y=0, apply=True)


class ColorMarkTests(TestCase):
    """Цвет как подпись: метка рядом с именем по всему сайту."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def render(self, person):
        return Template("{% load wall_extras %}{% wall_dot person %}").render(Context({"person": person}))

    def test_a_person_who_never_opened_the_wall_has_no_mark(self):
        self.assertEqual(self.render(self.user).strip(), "")

    def test_the_mark_carries_the_color(self):
        code = set_color(self.user, "основной синий")
        self.assertIn(palette.get(code).hex, self.render(User.objects.get(pk=self.user.pk)))

    def test_the_profile_page_shows_the_color_by_name(self):
        make_board()
        code = set_color(self.user, "тушь")
        response = self.client.get(reverse("profile", args=[self.user.pk]))
        self.assertContains(response, palette.get(code).hex)
        self.assertContains(response, "тушь")


class ViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.board = make_board()
        self.color = set_color(self.user, "основной синий")
        self.client.force_login(self.user)

    def test_the_page_opens(self):
        response = self.client.get(reverse("wall"))
        self.assertContains(response, "Стена")
        self.assertContains(response, palette.get(self.color).hex)  # палитра целиком на странице

    def test_the_page_carries_the_whole_palette(self):
        data = self.client.get(reverse("wall")).context["data"]
        self.assertEqual(len(data["colors"]), len(palette.PALETTE))
        self.assertEqual(data["neutral_from"], len(palette.PALETTE) - len(palette.NEUTRALS))
        self.assertFalse(data["own_color"])

    def test_the_page_needs_an_open_board(self):
        Board.objects.all().delete()
        self.assertEqual(self.client.get(reverse("wall")).status_code, 404)

    def test_history_carries_the_marks_before_the_events(self):
        put(self.user, self.board, 3, 2)
        response = self.client.get(reverse("wall_history"))
        self.assertEqual(int(response["X-Wall-Marks"]), 1)
        self.assertEqual(int(response["X-Wall-Step"]), MARK_EVERY)
        self.assertTrue(response["X-Wall-Start"])
        # четыре байта отметки, следом три байта события
        self.assertEqual(response.content, b"\0\0\0\0" + bytes((3, 2, self.color)))

    def test_snapshot_is_a_byte_per_cell_with_a_version(self):
        put(self.user, self.board, 3, 2)
        response = self.client.get(reverse("wall_snapshot"))
        self.assertEqual(len(response.content), 8 * 4)
        self.assertEqual(response.content[2 * 8 + 3], self.color)
        self.assertEqual(response["X-Wall-Version"], str(version(self.board)))

    def test_painting_through_the_view(self):
        response = self.client.post(reverse("wall_paint"), {"x": 1, "y": 1, "color": self.color})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["charges"], rules.MAX_CHARGES - 1)
        self.assertEqual(Pixel.objects.get(x=1, y=1).color, self.color)

    def test_running_out_answers_with_a_timer(self):
        for n in range(rules.MAX_CHARGES):
            put(self.user, self.board, *spread(self.board, n))
        response = self.client.post(reverse("wall_paint"), {"x": 7, "y": 3, "color": self.color})
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(response.json()["next"])

    def test_nonsense_coordinates_are_refused_not_crashed(self):
        for payload in ({"x": "тут", "y": 1}, {"y": 1}, {"x": 99, "y": 1}, {"x": 1, "y": 1}):
            response = self.client.post(reverse("wall_paint"), payload)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(Placement.objects.count(), 0)

    def test_erasing_through_the_view(self):
        put(self.user, self.board, 1, 1)
        response = self.client.post(reverse("wall_erase"), {"x": 1, "y": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Pixel.objects.get(x=1, y=1).color, palette.EMPTY)

    def test_reroll_without_money_is_refused(self):
        with mock.patch.object(rules, "OWN_COLOR_ONLY", True):
            response = self.client.post(reverse("wall_reroll"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(profile_of(self.user).color, self.color)

    def test_reroll_returns_the_new_color_and_the_rest_of_the_money(self):
        credit(self.user, rules.REROLL_PRICE + 25, MANUAL)
        with mock.patch.object(rules, "OWN_COLOR_ONLY", True):
            data = self.client.post(reverse("wall_reroll")).json()
        self.assertNotEqual(data["color"], self.color)
        self.assertEqual(data["balance"], 25)

    def card(self, x, y):
        return self.client.get(reverse("wall_pixel"), {"x": x, "y": y})

    def test_the_pixel_card_names_the_author(self):
        put(self.user, self.board, 1, 1)
        response = self.card(1, 1)
        self.assertContains(response, "Иван Иванов")
        self.assertContains(response, "закрасил")
        self.assertContains(response, 'data-mine="1"')

    def test_a_stranger_pixel_is_not_marked_as_mine(self):
        put(make_user("o@t.local"), self.board, 1, 1)
        self.assertContains(self.card(1, 1), 'data-mine="0"')

    def test_the_card_of_an_untouched_cell(self):
        self.assertContains(self.card(0, 0), "никто не трогал")

    def test_the_card_outside_the_board_is_404(self):
        self.assertEqual(self.card(9, 9).status_code, 404)
        self.assertEqual(self.client.get(reverse("wall_pixel")).status_code, 404)

    def test_a_stranger_is_not_let_in(self):
        self.client.logout()
        response = self.client.get(reverse("wall"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_moderator_tools_are_hidden_from_ordinary_people(self):
        response = self.client.get(reverse("wall"))
        self.assertNotContains(response, "Инструменты модератора")
        for name in ("wall_fill", "wall_rollback", "wall_protect", "wall_ban"):
            self.assertEqual(self.client.post(reverse(name), {}).status_code, 400)


class ModeratorViewTests(TestCase):
    def setUp(self):
        self.mod = make_moderator()
        self.board = make_board()
        self.color = set_color(self.mod, "основной синий")
        self.client.force_login(self.mod)

    def rect(self, **extra):
        return {"x1": 0, "y1": 0, "x2": 2, "y2": 1, **extra}

    def test_the_panel_shows_up_for_a_moderator(self):
        self.assertContains(self.client.get(reverse("wall")), "Инструменты модератора")

    def test_fill_through_the_view(self):
        response = self.client.post(reverse("wall_fill"), self.rect(color=self.color))
        self.assertEqual(response.json()["changed"], 6)
        self.assertEqual(Pixel.objects.exclude(color=palette.EMPTY).count(), 6)

    def test_painting_any_color_through_the_view(self):
        gold = next(c.code for c in palette.PICKABLE if c.name == "основной янтарный")
        self.client.post(reverse("wall_paint"), {"x": 1, "y": 1, "color": gold, "free": 1})
        self.assertEqual(Pixel.objects.get(x=1, y=1).color, gold)
        self.assertEqual(profile_of(self.mod).charges, rules.MAX_CHARGES)

    def test_freezing_and_unfreezing_through_the_view(self):
        added = self.client.post(reverse("wall_protect"), self.rect(note="герб")).json()
        self.assertEqual(len(added["areas"]), 1)
        left = self.client.post(reverse("wall_unprotect"), {"pk": added["added"]}).json()
        self.assertEqual(left["areas"], [])

    def test_rollback_through_the_view(self):
        self.client.post(reverse("wall_fill"), self.rect(color=self.color))
        response = self.client.post(reverse("wall_rollback"), self.rect(hours=1))
        self.assertEqual(response.json()["changed"], 6)
        self.assertEqual(set(snapshot(self.board)), {palette.EMPTY})

    def test_ban_through_the_view(self):
        grief = make_user("g@t.local")
        response = self.client.post(reverse("wall_ban"), {"user": grief.pk, "days": 7})
        self.assertIsNotNone(response.json()["until"])
        self.assertIsNotNone(profile_of(grief).banned_until)

    def test_absurd_numbers_from_the_form_are_clamped_not_crashed(self):
        deep = self.client.post(reverse("wall_rollback"), self.rect(hours=10 ** 9))
        self.assertEqual(deep.status_code, 200)
        forever = self.client.post(reverse("wall_ban"), {"user": self.mod.pk, "days": 10 ** 9})
        self.assertEqual(forever.status_code, 200)

    def test_a_broken_rectangle_is_refused_not_crashed(self):
        response = self.client.post(reverse("wall_fill"), {"x1": "тут", "y1": 0, "x2": 1, "y2": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("область", response.json()["error"])

    def test_the_area_arrives_as_one_message(self):
        with mock.patch("wall.services.notify_area") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                fill(self.mod, self.board, (0, 0, 2, 2), self.color)
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(len(notify.call_args[0][1]), 9)
