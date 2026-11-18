"""Всё, что меняет доску, идёт здесь: журнал и состояние в согласии.

Placement.user — автор состояния клетки после события. Мазок пишет того, кто рисовал; отмена
свои события из журнала удаляет; откат модератора дописывает события с прежними авторами.
Поэтому «состояние до события» — это предыдущая запись журнала по той же клетке.
"""

import logging
import random
from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone

from economy import rewards
from notifications import services as notices

from . import palette, rules
from .events import notify_area, notify_restore
from .models import FOREVER, Board, Pixel, Placement, ProtectedArea, WallProfile

logger = logging.getLogger(__name__)

MODERATOR = "wall.moderate_wall"
STAMPER = "wall.stamp_wall"
SKIP = 255  # в карте штампа: клетку не трогать (прозрачное)

FROZEN = "этот участок закрыт от правок"
STRANGER = "стереть можно только свой пиксель, чужой закрашивают"


class WallError(Exception):
    """Не дали, причина — в тексте."""


def profile_of(user):
    profile, _ = WallProfile.objects.get_or_create(user=user)
    return profile


# --- кисть и ластик ---


@transaction.atomic
def draw(user, board, cells):
    """Мазок: [(x, y, цвет)], цвет 0 — стереть. Закрытый участок и чужое стирание мазок не
    ломают: такие клетки возвращаются в rejected {(x, y): причина}, интерфейс вернёт им вид.
    Модератору можно всё. Возвращает (placements, rejected)."""
    profile = _ready(user, board)
    wanted = _cells(board, cells)
    moderator = user.has_perm(MODERATOR)
    rejected = {} if moderator else {at: FROZEN for at in _frozen(board, wanted)}
    erasing = [at for at, color in wanted.items() if color == palette.EMPTY and at not in rejected]
    if erasing and not moderator:
        now = _states(board, erasing)
        for at in erasing:
            color, owner = now.get(at, (palette.EMPTY, None))
            if color != palette.EMPTY and owner != user.pk:
                rejected[at] = STRANGER
    allowed = {at: color for at, color in wanted.items() if at not in rejected}
    _spend(user, len(allowed))
    placements = _write_many(board, allowed, user)
    _count(profile, sum(1 for placement in placements if placement.color != palette.EMPTY))
    return placements, rejected


def paint(user, board, x, y, color):
    """Одна клетка кистью; отказ — исключением."""
    _check_color(color)
    placements, rejected = draw(user, board, [(x, y, color)])
    if rejected:
        raise WallError(rejected[(x, y)])
    return placements[0] if placements else None


def erase(user, board, x, y):
    placements, rejected = draw(user, board, [(x, y, palette.EMPTY)])
    if rejected:
        raise WallError(rejected[(x, y)])
    return placements[0] if placements else None


@transaction.atomic
def undo(user, board, ids):
    """Отменить свои события (id из ответа на мазок): они уходят из журнала, клетка становится
    такой, какой была до них. Клетку, которую после них кто-то менял, не трогаем. Возвращает
    [(x, y, цвет, автор, когда)] вернувшихся клеток."""
    _ready(user, board)
    first, doomed = {}, defaultdict(set)
    for pk, x, y in board.placements.filter(pk__in=ids[: rules.STROKE_MAX], user=user).values_list("pk", "x", "y"):
        first[(x, y)] = min(first.get((x, y), pk), pk)
        doomed[(x, y)].add(pk)
    if not first:
        return []
    # Под замок до проверки: мазок посреди отмены ляжет после неё, а не под неё.
    list(_frame(board.pixels, list(first)).select_for_update().values_list("pk", flat=True))
    for pk, x, y in _frame(board.placements, list(first)).filter(id__gte=min(first.values())).values_list("pk", "x", "y"):
        if (x, y) in first and pk >= first[(x, y)] and pk not in doomed[(x, y)]:
            del first[(x, y)]
    _spend(user, len(first))
    now = timezone.now()
    back = []
    for (x, y), start in first.items():
        before = board.placements.filter(x=x, y=y, id__lt=start).order_by("-id").values_list("color", "user_id", "created").first()
        back.append((x, y, *(before or (palette.EMPTY, None, now))))
    board.placements.filter(pk__in=[pk for at in first for pk in doomed[at]]).delete()
    Pixel.objects.bulk_create(
        [Pixel(board=board, x=x, y=y, color=color, user_id=owner, placed=placed) for x, y, color, owner, placed in back],
        update_conflicts=True, update_fields=["color", "user", "placed"], unique_fields=["board", "x", "y"],
    )
    transaction.on_commit(lambda: notify_restore(board, back))
    return back


# --- инструменты модератора ---


@transaction.atomic
def fill(user, board, area, color):
    """Залить прямоугольник одним цветом. Пустой цвет — очистка."""
    require_moderator(user)
    x1, y1, x2, y2 = _area(board, area)
    _check_color(color, empty_too=True)
    cells = {(x, y): color for y in range(y1, y2 + 1) for x in range(x1, x2 + 1)}
    return _write_many(board, cells, user)


@transaction.atomic
def rollback(user, board, area, moment):
    """Вернуть прямоугольник к моменту — с прежними авторами."""
    require_moderator(user)
    x1, y1, x2, y2 = _area(board, area)
    was = {}
    events = (
        board.placements
        .filter(x__range=(x1, x2), y__range=(y1, y2), created__lte=moment)
        .order_by("id").values_list("x", "y", "color", "user_id")
    )
    for x, y, color, owner in events.iterator():
        was[(x, y)] = (color, owner)
    back = {
        (x, y): was.get((x, y), (palette.EMPTY, None))
        for y in range(y1, y2 + 1) for x in range(x1, x2 + 1)
    }
    return _restore(board, back)


@transaction.atomic
def rollback_person(user, board, person, since):
    """Убрать правки человека с момента since: каждая его клетка возвращается к тому, что было
    до его первой правки в ней. Где после него рисовал другой, не трогаем."""
    require_moderator(user)
    firsts = {
        (x, y): first for x, y, first in
        board.placements.filter(user=person, created__gte=since)
        .values_list("x", "y").annotate(first=Min("id")).values_list("x", "y", "first")
    }
    if not firsts:
        return []
    now = _states(board, list(firsts))
    firsts = {at: first for at, first in firsts.items() if now.get(at, (None, None))[1] == person.pk}
    was = {}
    if firsts:
        events = (
            board.placements.filter(id__lt=max(firsts.values()))
            .order_by("id").values_list("pk", "x", "y", "color", "user_id")
        )
        for pk, x, y, color, owner in events.iterator(chunk_size=5000):
            first = firsts.get((x, y))
            if first is not None and pk < first:
                was[(x, y)] = (color, owner)
    return _restore(board, {at: was.get(at, (palette.EMPTY, None)) for at in firsts})


def protect(user, board, area, note=""):
    """Закрыть участок от правок. Модератора это не касается."""
    require_moderator(user)
    x1, y1, x2, y2 = _area(board, area)
    return ProtectedArea.objects.create(
        board=board, x1=x1, y1=y1, x2=x2, y2=y2, note=note.strip()[:100], by=user,
    )


def unprotect(user, board, pk):
    require_moderator(user)
    ProtectedArea.objects.filter(board=board, pk=pk).delete()


@transaction.atomic
def ban(user, target, hours, reason=""):
    """Закрыть человеку доску на hours часов (-1 — навсегда, 0 — снять). Остальной сайт не трогаем.
    Сам человек узнаёт об этом колокольчиком."""
    require_moderator(user)
    profile = profile_of(target)
    if hours:
        profile.banned_until = FOREVER if hours < 0 else timezone.now() + timedelta(hours=hours)
        profile.ban_reason = reason.strip()[:200]
        profile.banned_by = user
    else:
        was = profile.banned
        profile.banned_until, profile.ban_reason, profile.banned_by = None, "", None
    profile.save(update_fields=["banned_until", "ban_reason", "banned_by"])
    if hours:
        term = "навсегда" if profile.forever else "до " + timezone.localtime(profile.banned_until).strftime("%d.%m %H:%M")
        notices.wall_banned(target, user, f"{term}{' · ' + profile.ban_reason if profile.ban_reason else ''}")
    elif was:
        notices.wall_unbanned(target, user)
    return profile


def bans():
    """Кому доска закрыта сейчас — для панели модератора."""
    return (
        WallProfile.objects.filter(banned_until__gt=timezone.now())
        .select_related("user", "banned_by").order_by("banned_until")
    )


# --- штамп картинки ---


def fit(width, height, rgba):
    """Картинку width×height (RGBA, по 4 байта) — в коды палитры: по байту на клетку, SKIP —
    прозрачное. Тот же подбор цвета, что у wall_draw: так подложку видят в цветах доски."""
    if not (0 < width <= 255 and 0 < height <= 255) or len(rgba) != width * height * 4:
        raise WallError("непонятная картинка")
    known = {}
    codes = bytearray(width * height)
    for index in range(width * height):
        red, green, blue, alpha = rgba[index * 4: index * 4 + 4]
        if alpha < 128:
            codes[index] = SKIP
            continue
        key = (red, green, blue)
        if key not in known:
            known[key] = palette.nearest(red, green, blue)
        codes[index] = known[key]
    return bytes(codes)


@transaction.atomic
def stamp(user, board, cells, only_empty=False):
    """Положить готовую картинку разом: {(x, y): цвет}. Без потолка и награды. Без права
    модератора закрытые участки обходятся. Клетки ложатся слоями по цветам."""
    if not user.has_perm(STAMPER) and not user.has_perm(MODERATOR):
        raise WallError("нужно право наносить картинки")
    for (x, y), color in cells.items():
        if not board.holds(x, y):
            raise WallError(f"клетка ({x}, {y}) вне доски")
        _check_color(color, empty_too=True)
    if not user.has_perm(MODERATOR):
        frozen = _frozen(board, cells)
        cells = {at: color for at, color in cells.items() if at not in frozen}
    if only_empty:
        now = _states(board, list(cells))
        cells = {at: color for at, color in cells.items() if now.get(at, (palette.EMPTY, None))[0] == palette.EMPTY}
    return _write_many(board, layered(cells), user)


def layered(cells):
    """Порядок записи (он же порядок таймлапса): слоями по цветам, крупные первыми, внутри
    слоя вперемешку — картинка проступает целиком, а не выезжает строками."""
    groups = defaultdict(list)
    for spot, code in cells.items():
        groups[code].append(spot)
    order = {}
    for code, spots in sorted(groups.items(), key=lambda pair: -len(pair[1])):
        random.shuffle(spots)
        for spot in spots:
            order[spot] = code
    return order


# --- чтение ---


def snapshot(board):
    """Доска одним куском: по байту на клетку, слева направо и сверху вниз."""
    buffer = bytearray(board.width * board.height)
    for x, y, color in board.pixels.exclude(color=palette.EMPTY).values_list("x", "y", "color"):
        buffer[y * board.width + x] = color
    return bytes(buffer)


def owners(board):
    """Кто и когда поставил каждую клетку: (люди [(id, имя)], номер человека в списке на клетку
    с единицы — 0 никто, секунды от открытия доски на клетку)."""
    people = {}
    who = [0] * (board.width * board.height)
    when = [0] * (board.width * board.height)
    for pixel in board.pixels.select_related("user").iterator(chunk_size=5000):
        if pixel.user is None:
            continue
        number = people.setdefault(pixel.user_id, (len(people) + 1, pixel.user.full_name))[0]
        index = pixel.y * board.width + pixel.x
        who[index] = number
        when[index] = max(0, int((pixel.placed - board.created).total_seconds()))
    return [(pk, name) for pk, (_, name) in sorted(people.items(), key=lambda pair: pair[1][0])], who, when


def version(board):
    """Номер последнего события: им клиент догоняет пропущенное после обрыва связи."""
    return board.placements.aggregate(last=Max("id"))["last"] or 0


# Время для подписи под ползунком таймлапса — у каждого N-го события.
MARK_EVERY = 256


def journal(board):
    """Весь журнал доски: отметки времени и события по три байта. Доску на момент собирает
    браузер — первые N событий на пустом полотне."""
    events = bytearray()
    marks = []
    start = None
    rows = board.placements.order_by("id").values_list("x", "y", "color", "created")
    for index, (x, y, color, created) in enumerate(rows.iterator(chunk_size=5000)):
        if start is None:
            start = created
        if index % MARK_EVERY == 0:
            marks.append(int((created - start).total_seconds()))
        events += bytes((x, y, color))
    return start, marks, bytes(events)


def history(board, x, y, limit=20):
    """Кто менял клетку, новое сверху, со стираниями."""
    return board.placements.filter(x=x, y=y).select_related("user")[:limit]


def require_moderator(user):
    """Публичная: вьюхам нужна до поиска чужих записей."""
    if not user.has_perm(MODERATOR):
        raise WallError("нужны права модератора Стены")


def require_admin(user):
    """Строже модератора: новая доска — конец сезона для всех."""
    if not user.is_staff:
        raise WallError("новую доску открывает только администратор")


@transaction.atomic
def open_board(user, title, width=None, height=None):
    """Закрыть нынешнюю доску и открыть новую того же размера; старая с журналом — в архив."""
    require_admin(user)
    title = title.strip()[:100]
    if not title:
        raise WallError("у доски должно быть название")
    now = timezone.now()
    closing = Board.objects.select_for_update().filter(is_active=True).first()
    if closing:
        closing.is_active = False
        closing.closed = now
        closing.save(update_fields=["is_active", "closed"])
    return Board.objects.create(
        title=title,
        width=width or (closing.width if closing else Board._meta.get_field("width").default),
        height=height or (closing.height if closing else Board._meta.get_field("height").default),
        created=now,
    )


# --- внутреннее ---


def _check_color(code, empty_too=False):
    if not (0 if empty_too else 1) <= code < len(palette.PALETTE):
        raise WallError("нет такого цвета")


def _cells(board, cells):
    """Мазок из запроса: проверенный и без повторов {(x, y): цвет}; повтор — последний цвет."""
    if len(cells) > rules.STROKE_MAX:
        raise WallError(f"за раз — не больше {rules.STROKE_MAX} клеток")
    wanted = {}
    for x, y, color in cells:
        if not board.holds(x, y):
            raise WallError("клетка вне доски")
        _check_color(color, empty_too=True)
        wanted[(x, y)] = color
    return wanted


def _area(board, area):
    """Прямоугольник из четырёх чисел: углы приводим к порядку, размер ограничиваем."""
    x1, x2 = sorted((area[0], area[2]))
    y1, y2 = sorted((area[1], area[3]))
    if not board.holds(x1, y1) or not board.holds(x2, y2):
        raise WallError("область выходит за доску")
    if (x2 - x1 + 1) * (y2 - y1 + 1) > rules.MAX_AREA:
        raise WallError(f"за раз можно взять не больше {rules.MAX_AREA} клеток")
    return x1, y1, x2, y2


def _frozen(board, cells):
    """Клетки из cells, лежащие в закрытых участках."""
    areas = list(board.areas.values_list("x1", "y1", "x2", "y2"))
    return {
        (x, y) for x, y in cells
        if any(x1 <= x <= x2 and y1 <= y <= y2 for x1, y1, x2, y2 in areas)
    }


def _frame(qs, cells):
    """Записи в рамке вокруг клеток: одним запросом, лишнее отсеивают по месту."""
    xs = [x for x, _ in cells]
    ys = [y for _, y in cells]
    return qs.filter(x__range=(min(xs), max(xs)), y__range=(min(ys), max(ys)))


def _states(board, cells):
    """Нынешнее {(x, y): (цвет, автор)} для перечня клеток — одним запросом по их рамке."""
    if not cells:
        return {}
    wanted = set(cells)
    rows = _frame(board.pixels, cells)
    return {
        (x, y): (color, owner)
        for x, y, color, owner in rows.values_list("x", "y", "color", "user_id")
        if (x, y) in wanted
    }


def _ready(user, board):
    """Общие проверки и запертый профиль: дальше его можно менять без гонок."""
    if not board.is_active:
        raise WallError("доска закрыта")
    profile_of(user)
    profile = WallProfile.objects.select_for_update().get(user=user)
    if profile.banned:
        raise WallError("доска для тебя закрыта")
    return profile


def _spend(user, cells):
    """Потолок от скриптов: rules.PER_MINUTE клеток в минуту. Кэш лёг — пропускаем."""
    if not cells or user.has_perm(MODERATOR):
        return
    key = f"wall:rate:{user.pk}"
    try:
        used = cells if cache.add(key, cells, 60) else cache.incr(key, cells)
    except ValueError:  # окно истекло между add и incr
        cache.set(key, cells, 60)
        used = cells
    except Exception:  # noqa: BLE001 — какой именно, зависит от бекенда кэша
        logger.exception("Кэш недоступен, потолок Стены пропускает")
        return
    if used > rules.PER_MINUTE:
        raise WallError("слишком быстро — подожди минуту")


def _count(profile, cells):
    """В счёт награды — не больше rules.COUNTED_PER_DAY клеток в сутки (по Москве)."""
    today = timezone.localdate()
    if profile.counted_on != today:
        profile.counted_on, profile.counted_today = today, 0
    add = min(cells, rules.COUNTED_PER_DAY - profile.counted_today)
    if add <= 0:
        return
    before = profile.painted
    profile.painted += add
    profile.counted_today += add
    profile.save(update_fields=["painted", "counted_on", "counted_today"])
    if profile.painted // rewards.WALL_BATCH > before // rewards.WALL_BATCH:
        rewards.sync(profile.user)


def _restore(board, back):
    """Вернуть клеткам состояния {(x, y): (цвет, автор)}."""
    colors = {at: color for at, (color, _) in back.items()}
    authors = {at: owner for at, (_, owner) in back.items()}
    return _write_many(board, colors, None, authors)


def _write_many(board, cells, user, authors=None):
    """Пачка клеток одним заходом: два запроса и одно событие в сокет. Порядок клеток — порядок
    журнала, то есть таймлапса. authors — свой автор у каждой клетки (отмена, откат)."""
    if not cells:
        return []
    now = timezone.now()
    current = _states(board, list(cells))
    if authors is None:
        # Кисть тем же цветом клетку не меняет — и авторство чужого рисунка не перехватывает.
        def owner(at):
            return user.pk
        changed = {at: color for at, color in cells.items() if current.get(at, (palette.EMPTY, None))[0] != color}
    else:
        owner = authors.get
        changed = {at: color for at, color in cells.items() if current.get(at, (palette.EMPTY, None)) != (color, owner(at))}
    if not changed:
        return []
    Pixel.objects.bulk_create(
        [Pixel(board=board, x=x, y=y, color=color, user_id=owner((x, y)), placed=now) for (x, y), color in changed.items()],
        update_conflicts=True, update_fields=["color", "user", "placed"], unique_fields=["board", "x", "y"],
    )
    placements = Placement.objects.bulk_create(
        [Placement(board=board, x=x, y=y, color=color, user_id=owner((x, y)), created=now) for (x, y), color in changed.items()]
    )
    transaction.on_commit(lambda: notify_area(board.pk, placements))
    return placements
