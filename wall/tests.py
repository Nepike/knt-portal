import base64
import json
import struct
import tempfile
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from economy.models import BalanceLog
from economy.rewards import WALL_BATCH
from notifications.models import Notification
from users.models import User

from . import palette, rules
from .events import notify_area
from .models import Board, Pixel, Placement, ProtectedArea, WallProfile
from .services import (
    FROZEN,
    MARK_EVERY,
    SKIP,
    STRANGER,
    WallError,
    ban,
    bans,
    draw,
    erase,
    fill,
    fit,
    history,
    journal,
    open_board,
    owners,
    paint,
    profile_of,
    protect,
    rollback,
    rollback_person,
    snapshot,
    stamp,
    undo,
    version,
)


def make_user(email="u@t.local"):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


def make_board(**extra):
    return Board.objects.create(title="Стена", width=8, height=4, **extra)


def grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))
    return User.objects.get(pk=user.pk)  # права кешируются на объекте, берём свежий


def make_moderator(email="mod@t.local"):
    return grant(make_user(email), "moderate_wall")


DEFAULT_COLOR = 1


def put(user, board, x, y, color=None):
    """Мазок в одну клетку: большинству проверок всё равно, каким цветом красили."""
    return paint(user, board, x, y, DEFAULT_COLOR if color is None else color)


def color(name):
    return next(c.code for c in palette.PICKABLE if c.name == name)


def state(x, y):
    pixel = Pixel.objects.get(x=x, y=y)
    return pixel.color, pixel.user_id


class WallCase(TestCase):
    def setUp(self):
        cache.clear()  # потолок мазков живёт в кэше и пережил бы тест
        self.addCleanup(cache.clear)


class PaletteTests(TestCase):
    def test_code_is_the_position_in_the_list(self):
        for index, color_ in enumerate(palette.PALETTE):
            self.assertEqual(color_.code, index)

    def test_the_palette_is_a_grid_a_neutral_row_and_a_bright_row(self):
        self.assertEqual(
            len(palette.PICKABLE),
            len(palette.TONES) * len(palette.HUES) + len(palette.NEUTRALS) + len(palette.BRIGHT),
        )
        self.assertEqual(palette.get(palette.EMPTY).name, "бетон")
        self.assertEqual(palette.get(palette.NEUTRAL_FROM).name, "тушь")
        self.assertEqual(palette.get(palette.BRIGHT_FROM).name, "чистый красный")
        self.assertEqual(palette.PALETTE[-1].name, "чистый розовый")

    def test_old_codes_do_not_move(self):
        """Цвет хранится номером: новые только дописываются, иначе старые рисунки перекрасились бы."""
        self.assertEqual((palette.get(1).hex, palette.get(1).name), ("#472724", "ночной красный"))
        self.assertEqual((palette.get(104).hex, palette.get(104).name), ("#FCF6EE", "мел"))

    def test_every_hue_has_the_whole_ladder_and_a_bright_one(self):
        """Рисуют лесенкой из соседних светлот одного тона — она должна быть целой."""
        for at, hue in enumerate(palette.HUES):
            ladder = [c.name for c in palette.PICKABLE[: palette.NEUTRAL_FROM - 1] if c.name.endswith(f" {hue}")]
            self.assertEqual(ladder, [f"{tone} {hue}" for tone in palette.TONES])
            self.assertEqual(palette.get(palette.BRIGHT_FROM + at).name, f"чистый {hue}")

    def test_names_are_unique(self):
        names = [c.name for c in palette.PALETTE]
        self.assertEqual(len(names), len(set(names)))


class BoardTests(TestCase):
    def test_only_one_board_can_be_current(self):
        make_board()
        with self.assertRaises(IntegrityError):
            make_board()

    def test_an_archived_board_does_not_block_a_new_one(self):
        make_board(is_active=False)
        make_board()
        self.assertEqual(Board.objects.count(), 2)

    def test_bounds(self):
        board = make_board()
        self.assertTrue(board.holds(0, 0))
        self.assertTrue(board.holds(7, 3))
        self.assertFalse(board.holds(8, 0))
        self.assertFalse(board.holds(-1, 0))


class StrokeTests(WallCase):
    """Мазок: без зарядов, с пропуском закрытого и чужого, с потолком от скриптов."""

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.board = Board.objects.create(title="Стена", width=16, height=8)
        self.red = color("основной красный")

    def test_a_stroke_lays_every_cell_in_order(self):
        placements, rejected = draw(self.user, self.board, [(1, 1, self.red), (2, 1, self.red), (3, 2, self.red)])
        self.assertEqual(rejected, {})
        self.assertEqual([(p.x, p.y) for p in placements], [(1, 1), (2, 1), (3, 2)])
        self.assertEqual(list(Placement.objects.order_by("id").values_list("x", "y")), [(1, 1), (2, 1), (3, 2)])
        self.assertEqual(state(2, 1), (self.red, self.user.pk))

    def test_there_is_no_limit_on_cells(self):
        for row in range(4):
            draw(self.user, self.board, [(x, row, self.red) for x in range(16)])
        self.assertEqual(Pixel.objects.exclude(color=palette.EMPTY).count(), 64)

    def test_a_repeated_cell_takes_its_last_color(self):
        green = color("основной зелёный")
        draw(self.user, self.board, [(1, 1, self.red), (1, 1, green)])
        self.assertEqual(state(1, 1), (green, self.user.pk))
        self.assertEqual(Placement.objects.count(), 1)

    def test_too_long_a_stroke_is_refused(self):
        cells = [(x % 16, x // 16 % 8, self.red) for x in range(rules.STROKE_MAX + 1)]
        with self.assertRaises(WallError):
            draw(self.user, self.board, cells)
        self.assertFalse(Placement.objects.exists())

    def test_a_cell_off_the_board_refuses_the_stroke(self):
        with self.assertRaises(WallError):
            draw(self.user, self.board, [(1, 1, self.red), (16, 0, self.red)])
        self.assertFalse(Placement.objects.exists())

    def test_a_color_outside_the_palette_is_refused(self):
        for bad in (999, -1, len(palette.PALETTE)):
            with self.assertRaises(WallError):
                draw(self.user, self.board, [(1, 1, bad)])

    def test_the_same_color_over_a_stranger_does_not_take_the_cell(self):
        """Кисть тем же цветом по чужому рисунку не меняет его автора и не пишет журнал."""
        other = make_user("o@t.local")
        draw(other, self.board, [(1, 1, self.red)])
        draw(self.user, self.board, [(1, 1, self.red)])
        self.assertEqual(state(1, 1), (self.red, other.pk))
        self.assertEqual(Placement.objects.count(), 1)

    def test_frozen_cells_are_skipped_not_fatal(self):
        protect(make_moderator(), self.board, (1, 1, 2, 2))
        placements, rejected = draw(self.user, self.board, [(0, 1, self.red), (1, 1, self.red), (3, 1, self.red)])
        self.assertEqual(rejected, {(1, 1): FROZEN})
        self.assertEqual(sorted((p.x, p.y) for p in placements), [(0, 1), (3, 1)])

    def test_a_stranger_cell_is_skipped_by_the_eraser(self):
        other = make_user("o@t.local")
        draw(other, self.board, [(1, 1, self.red)])
        draw(self.user, self.board, [(2, 1, self.red)])
        _, rejected = draw(self.user, self.board, [(1, 1, palette.EMPTY), (2, 1, palette.EMPTY)])
        self.assertEqual(rejected, {(1, 1): STRANGER})
        self.assertEqual(state(1, 1), (self.red, other.pk))
        self.assertEqual(state(2, 1), (palette.EMPTY, self.user.pk))

    def test_the_moderator_erases_and_paints_anything(self):
        mod = make_moderator()
        protect(mod, self.board, (1, 1, 1, 1))
        draw(self.user, self.board, [(2, 2, self.red)])
        _, rejected = draw(mod, self.board, [(1, 1, self.red), (2, 2, palette.EMPTY)])
        self.assertEqual(rejected, {})
        self.assertEqual(state(1, 1), (self.red, mod.pk))
        self.assertEqual(state(2, 2), (palette.EMPTY, mod.pk))

    def test_painting_too_fast_is_stopped(self):
        with mock.patch.object(rules, "PER_MINUTE", 3):
            draw(self.user, self.board, [(0, 0, self.red), (1, 0, self.red)])
            with self.assertRaises(WallError):
                draw(self.user, self.board, [(2, 0, self.red), (3, 0, self.red)])
        self.assertEqual(Placement.objects.count(), 2)

    def test_the_limit_is_per_person(self):
        with mock.patch.object(rules, "PER_MINUTE", 2):
            draw(self.user, self.board, [(0, 0, self.red), (1, 0, self.red)])
            draw(make_user("o@t.local"), self.board, [(2, 0, self.red), (3, 0, self.red)])
        self.assertEqual(Placement.objects.count(), 4)

    def test_the_moderator_is_not_limited(self):
        with mock.patch.object(rules, "PER_MINUTE", 1):
            draw(make_moderator(), self.board, [(x, 0, self.red) for x in range(5)])
        self.assertEqual(Placement.objects.count(), 5)

    def test_a_banned_person_is_refused(self):
        WallProfile.objects.create(user=self.user, banned_until=timezone.now() + timedelta(days=1))
        with self.assertRaises(WallError):
            draw(self.user, self.board, [(1, 1, self.red)])

    def test_an_expired_ban_lets_the_person_back(self):
        WallProfile.objects.create(user=self.user, banned_until=timezone.now() - timedelta(days=1))
        draw(self.user, self.board, [(1, 1, self.red)])
        self.assertEqual(Placement.objects.count(), 1)

    def test_a_closed_board_is_refused(self):
        Board.objects.filter(pk=self.board.pk).update(is_active=False)
        self.board.refresh_from_db()
        with self.assertRaises(WallError):
            draw(self.user, self.board, [(1, 1, self.red)])


class PaintTests(WallCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.board = make_board()

    def test_painting_writes_the_pixel_and_the_journal(self):
        put(self.user, self.board, 3, 2)
        self.assertEqual(state(3, 2), (DEFAULT_COLOR, self.user.pk))
        self.assertEqual(Placement.objects.count(), 1)

    def test_painting_over_a_stranger_replaces_the_owner(self):
        other = make_user("o@t.local")
        theirs = color("основной красный")
        put(other, self.board, 1, 1, theirs)
        put(self.user, self.board, 1, 1)
        self.assertEqual(state(1, 1), (DEFAULT_COLOR, self.user.pk))
        self.assertEqual([p.color for p in history(self.board, 1, 1)], [DEFAULT_COLOR, theirs])

    def test_a_frozen_cell_raises_for_one_cell(self):
        protect(make_moderator(), self.board, (1, 1, 1, 1))
        with self.assertRaises(WallError):
            put(self.user, self.board, 1, 1)
        self.assertFalse(Placement.objects.exists())

    def test_the_empty_color_is_not_a_paint(self):
        with self.assertRaises(WallError):
            put(self.user, self.board, 1, 1, palette.EMPTY)


class EraseTests(WallCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.board = make_board()

    def test_erasing_your_own_pixel_empties_the_cell(self):
        put(self.user, self.board, 2, 2)
        erase(self.user, self.board, 2, 2)
        self.assertEqual(state(2, 2), (palette.EMPTY, self.user.pk))

    def test_a_stranger_pixel_cannot_be_erased(self):
        put(make_user("o@t.local"), self.board, 2, 2)
        with self.assertRaises(WallError):
            erase(self.user, self.board, 2, 2)
        self.assertNotEqual(state(2, 2)[0], palette.EMPTY)

    def test_erasing_an_empty_cell_changes_nothing(self):
        self.assertIsNone(erase(self.user, self.board, 2, 2))
        self.assertFalse(Placement.objects.exists())

    def test_the_eraser_stays_in_the_history(self):
        put(self.user, self.board, 2, 2)
        erase(self.user, self.board, 2, 2)
        last = history(self.board, 2, 2)[0]
        self.assertEqual((last.color, last.user), (palette.EMPTY, self.user))

    def test_erasing_the_cell_you_painted_over_is_allowed(self):
        """Закрасил чужое своим цветом — пиксель стал твой, а свой стирать можно."""
        put(make_user("o@t.local"), self.board, 2, 2, color("основной красный"))
        put(self.user, self.board, 2, 2)
        erase(self.user, self.board, 2, 2)
        self.assertEqual(state(2, 2)[0], palette.EMPTY)


class RewardTests(WallCase):
    """В счёт награды — мазки кистью, не больше rules.COUNTED_PER_DAY клеток в сутки."""

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.board = Board.objects.create(title="Стена", width=16, height=16)
        self.red = color("основной красный")

    def fill_rows(self, rows, code=None):
        for row in rows:
            draw(self.user, self.board, [(x, row, code or self.red) for x in range(16)])

    def paid(self):
        return BalanceLog.objects.filter(wallet__user=self.user, reason=BalanceLog.Reason.WALL).count()

    def test_cells_count_up_to_the_daily_cap(self):
        self.fill_rows(range(8))  # 128 клеток
        profile = profile_of(self.user)
        self.assertEqual((profile.painted, profile.counted_today), (rules.COUNTED_PER_DAY, rules.COUNTED_PER_DAY))
        self.assertEqual(self.paid(), rules.COUNTED_PER_DAY // WALL_BATCH)

    def test_the_next_day_counts_again(self):
        self.fill_rows(range(8))
        with mock.patch("wall.services.timezone.localdate", return_value=timezone.localdate() + timedelta(days=1)):
            self.fill_rows(range(8, 10), color("основной зелёный"))
        self.assertEqual(profile_of(self.user).painted, rules.COUNTED_PER_DAY + 32)

    def test_erasing_does_not_count(self):
        self.fill_rows([0])
        draw(self.user, self.board, [(x, 0, palette.EMPTY) for x in range(16)])
        self.assertEqual(profile_of(self.user).painted, 16)

    def test_fills_and_stamps_do_not_count(self):
        mod = make_moderator()
        fill(mod, self.board, (0, 0, 15, 3), self.red)
        stamp(mod, self.board, {(x, 5): self.red for x in range(16)})
        self.assertEqual(profile_of(mod).painted, 0)


class UndoTests(WallCase):
    """Отмена своего мазка возвращает клетке прежний вид и прежнего автора."""

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.other = make_user("o@t.local")
        self.board = make_board()
        self.red = color("основной красный")
        self.green = color("основной зелёный")

    def ids(self, placements):
        return [p.pk for p in placements]

    def test_undo_returns_the_cell_and_its_author(self):
        draw(self.other, self.board, [(1, 1, self.red)])
        mine, _ = draw(self.user, self.board, [(1, 1, self.green), (2, 1, self.green)])
        undo(self.user, self.board, self.ids(mine))
        self.assertEqual(state(1, 1), (self.red, self.other.pk))
        self.assertEqual(state(2, 1), (palette.EMPTY, None))

    def test_a_cell_someone_changed_since_is_left_alone(self):
        mine, _ = draw(self.user, self.board, [(1, 1, self.green)])
        draw(self.other, self.board, [(1, 1, self.red)])
        undo(self.user, self.board, self.ids(mine))
        self.assertEqual(state(1, 1), (self.red, self.other.pk))

    def test_only_ones_own_events_are_undone(self):
        """Даже когда клетка сейчас моя и того же цвета, что в чужом событии."""
        theirs, _ = draw(self.other, self.board, [(1, 1, self.red)])
        draw(self.user, self.board, [(1, 1, self.green)])
        draw(self.user, self.board, [(1, 1, self.red)])
        undo(self.user, self.board, self.ids(theirs))
        self.assertEqual(state(1, 1), (self.red, self.user.pk))

    def test_undo_after_undo_goes_further_back(self):
        first, _ = draw(self.user, self.board, [(1, 1, self.red)])
        second, _ = draw(self.user, self.board, [(1, 1, self.green)])
        undo(self.user, self.board, self.ids(second))
        self.assertEqual(state(1, 1), (self.red, self.user.pk))
        undo(self.user, self.board, self.ids(first))
        self.assertEqual(state(1, 1), (palette.EMPTY, None))

    def test_several_events_on_one_cell_go_back_to_before_the_first(self):
        first, _ = draw(self.user, self.board, [(1, 1, self.red)])
        second, _ = draw(self.user, self.board, [(1, 1, self.green)])
        undo(self.user, self.board, self.ids(first) + self.ids(second))
        self.assertEqual(state(1, 1), (palette.EMPTY, None))

    def test_undo_writes_the_journal_with_the_author_of_the_state(self):
        draw(self.other, self.board, [(1, 1, self.red)])
        mine, _ = draw(self.user, self.board, [(1, 1, self.green)])
        undo(self.user, self.board, self.ids(mine))
        last = Placement.objects.latest("id")
        self.assertEqual((last.color, last.user_id), (self.red, self.other.pk))


class SnapshotTests(WallCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.board = make_board()

    def test_snapshot_is_one_byte_per_cell(self):
        self.assertEqual(len(snapshot(self.board)), 8 * 4)

    def test_the_cell_lands_in_its_place(self):
        put(self.user, self.board, 3, 2)
        self.assertEqual(snapshot(self.board)[2 * 8 + 3], DEFAULT_COLOR)

    def test_an_erased_cell_goes_back_to_zero(self):
        put(self.user, self.board, 3, 2)
        erase(self.user, self.board, 3, 2)
        self.assertEqual(set(snapshot(self.board)), {palette.EMPTY})

    def test_version_follows_the_last_event(self):
        self.assertEqual(version(self.board), 0)
        last = put(self.user, self.board, 0, 0)
        self.assertEqual(version(self.board), last.pk)


class OwnersTests(WallCase):
    """Кто и когда поставил каждую клетку — для подсказки «кто» без запросов."""

    def test_owners_number_the_people_and_time_the_cells(self):
        board = make_board()
        first, second = make_user("a@t.local"), make_user("b@t.local")
        User.objects.filter(pk=second.pk).update(name="Пётр", surname="Петров")
        put(first, board, 0, 0)
        put(second, board, 3, 1)
        Pixel.objects.filter(x=3, y=1).update(placed=board.created + timedelta(seconds=90))
        people, who, when = owners(board)
        self.assertEqual([name for _, name in people], ["Иванов Иван", "Петров Пётр"])
        self.assertEqual(people[who[1 * 8 + 3] - 1][0], second.pk)
        self.assertEqual(who[0], 1)
        self.assertEqual(who[1], 0)
        self.assertEqual(when[1 * 8 + 3], 90)


class NewBoardTests(WallCase):
    def setUp(self):
        super().setUp()
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
        self.assertEqual(self.client.post(reverse("wall_new"), {"title": "Весна"}).status_code, 200)
        self.assertEqual(Board.current().title, "Весна")

    def test_an_ordinary_person_is_refused_by_the_view(self):
        self.client.force_login(make_user("o@t.local"))
        self.assertEqual(self.client.post(reverse("wall_new"), {"title": "Моя"}).status_code, 400)
        self.assertEqual(Board.objects.count(), 1)


class JournalTests(WallCase):
    """Журнал таймлапса: события подряд на пустом полотне дают ровно доску снимка."""

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.board = make_board()

    def replay(self, events):
        cells = bytearray(self.board.width * self.board.height)
        for at in range(0, len(events), 3):
            x, y, code = events[at:at + 3]
            cells[y * self.board.width + x] = code
        return bytes(cells)

    def test_three_bytes_per_event_in_order(self):
        put(self.user, self.board, 3, 2)
        put(self.user, self.board, 0, 0)
        _, _, events = journal(self.board)
        self.assertEqual(events, bytes((3, 2, DEFAULT_COLOR, 0, 0, DEFAULT_COLOR)))

    def test_replaying_the_journal_gives_the_snapshot(self):
        mod = make_moderator("m@t.local")
        for x, y in ((1, 1), (2, 1), (3, 3)):
            put(self.user, self.board, x, y)
        erase(self.user, self.board, 2, 1)
        mine, _ = draw(self.user, self.board, [(5, 3, color("тушь"))])
        undo(self.user, self.board, [p.pk for p in mine])
        fill(mod, self.board, (4, 0, 6, 1), color("тушь"))
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


class AreaTests(WallCase):
    def setUp(self):
        super().setUp()
        self.mod = make_moderator()
        self.board = make_board()

    def test_fill_paints_the_whole_rectangle(self):
        fill(self.mod, self.board, (1, 1, 3, 2), DEFAULT_COLOR)
        painted = Pixel.objects.exclude(color=palette.EMPTY).values_list("x", "y")
        self.assertEqual(sorted(painted), [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2)])

    def test_corners_may_come_in_any_order(self):
        fill(self.mod, self.board, (3, 2, 1, 1), DEFAULT_COLOR)
        self.assertEqual(Pixel.objects.exclude(color=palette.EMPTY).count(), 6)

    def test_cells_that_already_match_are_left_alone(self):
        fill(self.mod, self.board, (0, 0, 1, 1), DEFAULT_COLOR)
        fill(self.mod, self.board, (0, 0, 1, 1), DEFAULT_COLOR)
        self.assertEqual(Placement.objects.count(), 4)  # второй заход ничего не менял

    def test_clearing_is_the_same_fill_with_an_empty_color(self):
        fill(self.mod, self.board, (0, 0, 2, 2), DEFAULT_COLOR)
        fill(self.mod, self.board, (0, 0, 2, 2), palette.EMPTY)
        self.assertEqual(set(snapshot(self.board)), {palette.EMPTY})

    def test_an_area_beyond_the_board_is_refused(self):
        with self.assertRaises(WallError):
            fill(self.mod, self.board, (0, 0, 99, 99), DEFAULT_COLOR)

    def test_too_large_an_area_is_refused(self):
        board = Board.objects.create(title="Большая", width=200, height=200, is_active=False)
        with self.assertRaises(WallError):
            fill(self.mod, board, (0, 0, 199, 199), DEFAULT_COLOR)

    def test_an_ordinary_person_has_no_fill(self):
        with self.assertRaises(WallError):
            fill(make_user("o@t.local"), self.board, (0, 0, 1, 1), DEFAULT_COLOR)


class RollbackTests(WallCase):
    def setUp(self):
        super().setUp()
        self.mod = make_moderator()
        self.board = make_board()
        self.author = make_user("a@t.local")
        self.was = color("основной зелёный")

    def age(self, hours):
        moment = timezone.now() - timedelta(hours=hours)
        Placement.objects.update(created=moment)
        Pixel.objects.update(placed=moment)

    def test_rollback_restores_what_was_under_the_paint_with_its_author(self):
        put(self.author, self.board, 1, 1, self.was)
        self.age(2)
        grief = make_user("g@t.local")
        put(grief, self.board, 1, 1)
        put(grief, self.board, 2, 2)
        rollback(self.mod, self.board, (0, 0, 3, 3), timezone.now() - timedelta(hours=1))
        self.assertEqual(state(1, 1), (self.was, self.author.pk))
        self.assertEqual(state(2, 2), (palette.EMPTY, None))

    def test_an_ordinary_person_has_no_rollback(self):
        with self.assertRaises(WallError):
            rollback(make_user("o@t.local"), self.board, (0, 0, 1, 1), timezone.now())


class RollbackPersonTests(WallCase):
    """Откат правок одного человека: его клетки — к тому, что было до него."""

    def setUp(self):
        super().setUp()
        self.mod = make_moderator()
        self.board = make_board()
        self.author = make_user("a@t.local")
        self.grief = make_user("g@t.local")
        self.green = color("основной зелёный")
        put(self.author, self.board, 1, 1, self.green)
        put(self.grief, self.board, 5, 3)  # давняя работа того же человека
        Placement.objects.update(created=timezone.now() - timedelta(hours=3))

    def since(self):
        return timezone.now() - timedelta(hours=1)

    def test_the_persons_cells_go_back_with_their_authors(self):
        put(self.grief, self.board, 1, 1)
        put(self.grief, self.board, 2, 2)
        rollback_person(self.mod, self.board, self.grief, self.since())
        self.assertEqual(state(1, 1), (self.green, self.author.pk))
        self.assertEqual(state(2, 2), (palette.EMPTY, None))

    def test_older_work_of_the_same_person_stays(self):
        put(self.grief, self.board, 1, 1)
        rollback_person(self.mod, self.board, self.grief, self.since())
        self.assertEqual(state(5, 3), (DEFAULT_COLOR, self.grief.pk))

    def test_cells_painted_over_since_are_left_alone(self):
        put(self.grief, self.board, 1, 1)
        put(self.author, self.board, 1, 1, color("основной красный"))
        rollback_person(self.mod, self.board, self.grief, self.since())
        self.assertEqual(state(1, 1), (color("основной красный"), self.author.pk))

    def test_a_cell_he_touched_twice_goes_back_to_before_the_first(self):
        put(self.grief, self.board, 1, 1)
        put(self.grief, self.board, 1, 1, color("основной красный"))
        put(self.grief, self.board, 2, 2)  # и потом трогал другую клетку
        rollback_person(self.mod, self.board, self.grief, self.since())
        self.assertEqual(state(1, 1), (self.green, self.author.pk))

    def test_only_a_moderator(self):
        with self.assertRaises(WallError):
            rollback_person(make_user("o@t.local"), self.board, self.grief, self.since())


class ProtectedAreaTests(WallCase):
    def setUp(self):
        super().setUp()
        self.mod = make_moderator()
        self.board = make_board()
        self.person = make_user("p@t.local")
        protect(self.mod, self.board, (1, 1, 3, 3), note="герб")

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


class BanTests(WallCase):
    def setUp(self):
        super().setUp()
        self.mod = make_moderator()
        self.board = make_board()
        self.grief = make_user("g@t.local")

    def test_ban_closes_the_board_but_not_the_site(self):
        ban(self.mod, self.grief, 24 * 7)
        with self.assertRaises(WallError):
            put(self.grief, self.board, 1, 1)
        self.assertTrue(User.objects.get(pk=self.grief.pk).is_active)

    def test_zero_lifts_the_ban(self):
        ban(self.mod, self.grief, 24)
        ban(self.mod, self.grief, 0)
        put(self.grief, self.board, 1, 1)
        self.assertEqual(Placement.objects.count(), 1)

    def test_forever_is_forever(self):
        profile = ban(self.mod, self.grief, -1, "скрипт")
        self.assertTrue(profile.forever)
        self.assertTrue(profile.banned)
        self.assertEqual((profile.ban_reason, profile.banned_by), ("скрипт", self.mod))

    def test_the_person_hears_of_it_with_the_term_and_the_reason(self):
        ban(self.mod, self.grief, 24, "закрашивал чужое")
        notice = Notification.objects.get(recipient=self.grief)
        self.assertEqual(notice.kind, Notification.Kind.WALL_BAN)
        self.assertIn("до ", notice.note)
        self.assertIn("закрашивал чужое", notice.note)
        self.assertEqual(notice.target_url(), reverse("wall"))

    def test_lifting_is_heard_too_but_only_a_real_one(self):
        ban(self.mod, self.grief, 0)  # бана и не было
        self.assertFalse(Notification.objects.exists())
        ban(self.mod, self.grief, 24)
        ban(self.mod, self.grief, 0)
        self.assertEqual(
            list(Notification.objects.order_by("id").values_list("kind", flat=True)),
            [Notification.Kind.WALL_BAN, Notification.Kind.WALL_UNBAN],
        )

    def test_bans_list_only_the_current_ones(self):
        ban(self.mod, self.grief, 24)
        old = make_user("old@t.local")
        WallProfile.objects.create(user=old, banned_until=timezone.now() - timedelta(days=1))
        self.assertEqual([profile.user for profile in bans()], [self.grief])

    def test_an_ordinary_person_cannot_ban(self):
        with self.assertRaises(WallError):
            ban(make_user("o@t.local"), self.grief, 24)

    def test_the_notification_reads_as_a_wall_ban(self):
        ban(self.mod, self.grief, 24, "мат буквами")
        self.client.force_login(self.grief)
        response = self.client.get(reverse("notification_list"))
        self.assertContains(response, "Стена для вас закрыта")
        self.assertContains(response, "мат буквами")


class BroadcastTests(WallCase):
    def test_a_stroke_is_announced_once_and_only_after_the_commit(self):
        user = make_user()
        board = make_board()
        with mock.patch("wall.services.notify_area") as notify:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                placements, _ = draw(user, board, [(1, 1, DEFAULT_COLOR), (2, 1, DEFAULT_COLOR)])
                notify.assert_not_called()  # транзакция ещё не закрыта
            self.assertEqual(len(callbacks), 1)
            notify.assert_called_once_with(board.pk, placements)

    def test_a_refused_stroke_is_not_announced(self):
        with mock.patch("wall.services.notify_area") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(WallError):
                    draw(make_user(), make_board(), [(99, 99, DEFAULT_COLOR)])
            notify.assert_not_called()

    def test_the_message_carries_the_authors_and_their_names(self):
        user = make_user()
        board = make_board()
        placements, _ = draw(user, board, [(1, 1, DEFAULT_COLOR)])
        with mock.patch("wall.events._publish") as publish:
            notify_area(board.pk, placements)
        payload = publish.call_args[0][1]
        self.assertEqual(payload["pixels"], [[1, 1, DEFAULT_COLOR, user.pk]])
        self.assertEqual(payload["people"], {str(user.pk): "Иванов Иван"})


class FitTests(TestCase):
    """Картинка — в коды палитры: тот же подбор, что у wall_draw."""

    def test_colors_land_on_the_palette_and_transparency_is_skipped(self):
        red = palette.rgb(palette.get(color("основной красный")).hex)
        rgba = bytes((*red, 255, 0, 0, 0, 0))
        self.assertEqual(fit(2, 1, rgba), bytes((color("основной красный"), SKIP)))

    def test_a_wrong_size_or_a_huge_picture_is_refused(self):
        with self.assertRaises(WallError):
            fit(2, 1, bytes(4))
        with self.assertRaises(WallError):
            fit(256, 1, bytes(256 * 4))


class StampTests(WallCase):
    def setUp(self):
        super().setUp()
        self.board = make_board()
        self.stamper = grant(make_user("s@t.local"), "stamp_wall")
        self.red = color("основной красный")

    def test_a_stamp_needs_its_right(self):
        with self.assertRaises(WallError):
            stamp(make_user("o@t.local"), self.board, {(1, 1): self.red})
        stamp(self.stamper, self.board, {(1, 1): self.red})
        stamp(make_moderator(), self.board, {(2, 1): self.red})
        self.assertEqual(Placement.objects.count(), 2)

    def test_only_empty_keeps_others_work(self):
        theirs = make_user("o@t.local")
        put(theirs, self.board, 1, 1)
        stamp(self.stamper, self.board, {(1, 1): self.red, (2, 1): self.red}, only_empty=True)
        self.assertEqual(state(1, 1), (DEFAULT_COLOR, theirs.pk))
        self.assertEqual(state(2, 1), (self.red, self.stamper.pk))

    def test_a_stamper_goes_around_frozen_areas_a_moderator_does_not(self):
        protect(make_moderator(), self.board, (1, 1, 1, 1))
        stamp(self.stamper, self.board, {(1, 1): self.red, (2, 1): self.red})
        self.assertFalse(Pixel.objects.filter(x=1, y=1).exists())
        stamp(make_moderator("m2@t.local"), self.board, {(1, 1): self.red})
        self.assertEqual(state(1, 1)[0], self.red)

    def test_a_cell_off_the_board_refuses_the_stamp(self):
        with self.assertRaises(WallError):
            stamp(self.stamper, self.board, {(9, 9): self.red})


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
        """Приглушённый цвет уходит к соседям по кругу, а не наугад."""
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
        """Порядок записи — порядок таймлапса: слоями, а не строками."""
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


class ViewTests(WallCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.board = make_board()
        self.client.force_login(self.user)

    def post_json(self, name, value):
        return self.client.post(reverse(name), json.dumps(value), content_type="application/json")

    def test_the_page_opens_with_the_whole_palette(self):
        response = self.client.get(reverse("wall"))
        self.assertContains(response, "Стена")
        data = response.context["data"]
        self.assertEqual(len(data["colors"]), len(palette.PALETTE))
        self.assertEqual((data["neutral_from"], data["bright_from"]), (palette.NEUTRAL_FROM, palette.BRIGHT_FROM))
        self.assertEqual(data["can"], {"moderate": False, "stamp": False, "admin": False})
        self.assertIsNone(data["banned"]["until"])

    def test_the_page_tells_a_banned_person_why(self):
        WallProfile.objects.create(user=self.user, banned_until=timezone.now() + timedelta(hours=2), ban_reason="мат")
        banned = self.client.get(reverse("wall")).context["data"]["banned"]
        self.assertEqual((banned["reason"], banned["forever"]), ("мат", False))
        self.assertIsNotNone(banned["until"])

    def test_the_page_needs_an_open_board(self):
        Board.objects.all().delete()
        self.assertEqual(self.client.get(reverse("wall")).status_code, 404)

    def test_history_carries_the_marks_before_the_events(self):
        put(self.user, self.board, 3, 2)
        response = self.client.get(reverse("wall_history"))
        self.assertEqual(int(response["X-Wall-Marks"]), 1)
        self.assertEqual(int(response["X-Wall-Step"]), MARK_EVERY)
        self.assertTrue(response["X-Wall-Start"])
        self.assertEqual(response.content, b"\0\0\0\0" + bytes((3, 2, DEFAULT_COLOR)))

    def test_snapshot_is_a_byte_per_cell_with_a_version(self):
        put(self.user, self.board, 3, 2)
        response = self.client.get(reverse("wall_snapshot"))
        self.assertEqual(len(response.content), 8 * 4)
        self.assertEqual(response.content[2 * 8 + 3], DEFAULT_COLOR)
        self.assertEqual(response["X-Wall-Version"], str(version(self.board)))

    def test_owners_come_as_little_endian_arrays(self):
        put(self.user, self.board, 3, 2)
        data = self.client.get(reverse("wall_owners")).json()
        who = struct.unpack("<32H", base64.b64decode(data["who"]))
        when = struct.unpack("<32I", base64.b64decode(data["when"]))
        self.assertEqual(data["people"], [[self.user.pk, "Иванов Иван"]])
        self.assertEqual((who[2 * 8 + 3], who[0]), (1, 0))
        self.assertEqual(len(when), 32)
        self.assertEqual(data["version"], version(self.board))

    def test_a_stroke_through_the_view(self):
        response = self.post_json("wall_draw", {"cells": [[1, 1, DEFAULT_COLOR], [2, 1, DEFAULT_COLOR]]})
        data = response.json()
        self.assertEqual(len(data["ids"]), 2)
        self.assertEqual(data["rejected"], [])
        self.assertEqual(state(2, 1), (DEFAULT_COLOR, self.user.pk))

    def test_rejected_cells_come_back_with_the_reason(self):
        put(make_user("o@t.local"), self.board, 1, 1)
        data = self.post_json("wall_draw", {"cells": [[1, 1, palette.EMPTY]]}).json()
        self.assertEqual((data["rejected"], data["reason"]), ([[1, 1]], STRANGER))

    def test_nonsense_strokes_are_refused_not_crashed(self):
        for payload in ({}, {"cells": "тут"}, {"cells": [[1, "y", 1]]}, {"cells": [[1, 1]]}, {"cells": [[99, 1, 1]]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post_json("wall_draw", payload).status_code, 400)
        self.assertEqual(self.client.post(reverse("wall_draw"), "{не json", content_type="application/json").status_code, 400)
        self.assertFalse(Placement.objects.exists())

    def test_a_banned_person_gets_the_refusal(self):
        WallProfile.objects.create(user=self.user, banned_until=timezone.now() + timedelta(hours=2))
        response = self.post_json("wall_draw", {"cells": [[1, 1, DEFAULT_COLOR]]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("закрыта", response.json()["error"])

    def test_undo_through_the_view(self):
        ids = self.post_json("wall_draw", {"cells": [[1, 1, DEFAULT_COLOR]]}).json()["ids"]
        self.assertEqual(self.post_json("wall_undo", {"ids": ids}).json()["changed"], 1)
        self.assertEqual(state(1, 1), (palette.EMPTY, None))
        self.assertEqual(self.post_json("wall_undo", {"ids": "все"}).status_code, 400)

    def card(self, x, y):
        return self.client.get(reverse("wall_pixel"), {"x": x, "y": y})

    def test_the_pixel_card_names_the_author_and_the_history(self):
        put(make_user("o@t.local"), self.board, 1, 1, color("основной красный"))
        put(self.user, self.board, 1, 1)
        data = self.card(1, 1).json()
        self.assertEqual(data["owner"]["name"], "Иванов Иван")
        self.assertEqual(data["owner"]["url"], reverse("profile", args=[self.user.pk]))
        self.assertEqual([event["color"] for event in data["events"]], [DEFAULT_COLOR, color("основной красный")])

    def test_the_card_of_an_untouched_cell(self):
        data = self.card(0, 0).json()
        self.assertEqual((data["owner"], data["events"], data["color"]), (None, [], palette.EMPTY))

    def test_the_card_outside_the_board_is_404(self):
        self.assertEqual(self.card(9, 9).status_code, 404)
        self.assertEqual(self.client.get(reverse("wall_pixel")).status_code, 404)

    def test_fitting_a_picture_answers_with_palette_codes(self):
        red = palette.rgb(palette.get(color("основной красный")).hex)
        rgba = base64.b64encode(bytes((*red, 255, 0, 0, 0, 0))).decode()
        codes = self.post_json("wall_fit", {"w": 2, "h": 1, "rgba": rgba}).json()["codes"]
        self.assertEqual(base64.b64decode(codes), bytes((color("основной красный"), SKIP)))

    def test_fitting_is_throttled(self):
        payload = {"w": 1, "h": 1, "rgba": base64.b64encode(bytes(4)).decode()}
        with mock.patch("wall.views.FITS_PER_WINDOW", 1):
            self.post_json("wall_fit", payload)
            self.assertEqual(self.post_json("wall_fit", payload).status_code, 429)

    def stamp_payload(self, **extra):
        codes = bytes((color("основной красный"), SKIP, color("основной красный"), SKIP))
        return {"x": 1, "y": 1, "w": 2, "h": 2, "codes": base64.b64encode(codes).decode(), **extra}

    def test_a_stamp_needs_its_right(self):
        response = self.post_json("wall_stamp", self.stamp_payload())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Placement.objects.exists())

    def test_a_stamp_through_the_view(self):
        self.client.force_login(grant(self.user, "stamp_wall"))
        self.assertEqual(self.post_json("wall_stamp", self.stamp_payload()).json()["changed"], 2)
        self.assertEqual(sorted(Pixel.objects.values_list("x", "y")), [(1, 1), (1, 2)])
        self.assertEqual(self.post_json("wall_stamp", self.stamp_payload(w=3)).status_code, 400)

    def test_a_stranger_is_not_let_in(self):
        self.client.logout()
        response = self.client.get(reverse("wall"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_moderator_tools_are_closed_to_ordinary_people(self):
        for name in ("wall_fill", "wall_rollback", "wall_rollback_person", "wall_protect", "wall_ban"):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name), {"user": self.user.pk}).status_code, 400)
        self.assertEqual(self.client.get(reverse("wall_bans")).status_code, 403)


class ModeratorViewTests(WallCase):
    def setUp(self):
        super().setUp()
        self.mod = make_moderator()
        self.board = make_board()
        self.client.force_login(self.mod)

    def rect(self, **extra):
        return {"x1": 0, "y1": 0, "x2": 2, "y2": 1, **extra}

    def test_the_page_lets_a_moderator_in(self):
        can = self.client.get(reverse("wall")).context["data"]["can"]
        self.assertEqual(can, {"moderate": True, "stamp": True, "admin": False})

    def test_fill_through_the_view(self):
        response = self.client.post(reverse("wall_fill"), self.rect(color=DEFAULT_COLOR))
        self.assertEqual(response.json()["changed"], 6)

    def test_freezing_and_unfreezing_through_the_view(self):
        added = self.client.post(reverse("wall_protect"), self.rect(note="герб")).json()
        self.assertEqual(len(added["areas"]), 1)
        left = self.client.post(reverse("wall_unprotect"), {"pk": added["added"]}).json()
        self.assertEqual(left["areas"], [])

    def test_rollback_through_the_view(self):
        self.client.post(reverse("wall_fill"), self.rect(color=DEFAULT_COLOR))
        response = self.client.post(reverse("wall_rollback"), self.rect(hours=1))
        self.assertEqual(response.json()["changed"], 6)
        self.assertEqual(set(snapshot(self.board)), {palette.EMPTY})

    def test_ban_with_a_term_and_a_reason_through_the_view(self):
        grief = make_user("g@t.local")
        data = self.client.post(reverse("wall_ban"), {"user": grief.pk, "hours": 24, "reason": "спам"}).json()
        self.assertEqual((data["who"], data["reason"], data["forever"]), ("Иванов Иван", "спам", False))
        self.assertTrue(profile_of(grief).banned)
        listed = self.client.get(reverse("wall_bans")).json()["bans"]
        self.assertEqual([(row["id"], row["reason"], row["by"]) for row in listed], [(grief.pk, "спам", "Иванов Иван")])

    def test_only_the_offered_terms(self):
        grief = make_user("g@t.local")
        response = self.client.post(reverse("wall_ban"), {"user": grief.pk, "hours": 10 ** 9})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(profile_of(grief).banned)

    def test_rollback_of_a_person_through_the_view(self):
        grief = make_user("g@t.local")
        put(grief, self.board, 1, 1)
        response = self.client.post(reverse("wall_rollback_person"), {"user": grief.pk, "hours": 1})
        self.assertEqual(response.json()["changed"], 1)
        self.assertEqual(state(1, 1), (palette.EMPTY, None))

    def test_absurd_numbers_from_the_form_are_clamped_not_crashed(self):
        deep = self.client.post(reverse("wall_rollback"), self.rect(hours=10 ** 9))
        self.assertEqual(deep.status_code, 200)

    def test_a_broken_rectangle_is_refused_not_crashed(self):
        response = self.client.post(reverse("wall_fill"), {"x1": "тут", "y1": 0, "x2": 1, "y2": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("область", response.json()["error"])

    def test_the_area_arrives_as_one_message(self):
        with mock.patch("wall.services.notify_area") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                fill(self.mod, self.board, (0, 0, 2, 2), DEFAULT_COLOR)
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(len(notify.call_args[0][1]), 9)
