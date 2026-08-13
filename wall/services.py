"""Работа с доской: заряды, закраска, стирание, смена цвета, инструменты модератора.

Всё, что меняет доску, идёт через эти функции — они держат журнал и нынешнее
состояние в согласии и считают заряды по часам сервера, а не по обещаниям браузера.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from economy.models import BalanceLog
from economy.services import spend

from . import palette, rules
from .events import notify_area, notify_pixel
from .models import Board, Pixel, Placement, ProtectedArea, WallProfile

MODERATOR = "wall.moderate_wall"


class WallError(Exception):
    """Поставить не дали, причина — в тексте."""


class NoCharges(WallError):
    """Пиксели кончились. Отдельный класс: интерфейсу тут надо показать таймер."""


def profile_of(user):
    profile, _ = WallProfile.objects.get_or_create(user=user, defaults={"color": palette.roll()})
    return profile


def status(profile, now=None):
    """Сколько зарядов сейчас и когда придёт следующий (None — копилка полна)."""
    now = now or timezone.now()
    _settle(profile, now)
    if profile.charges >= rules.MAX_CHARGES:
        return profile.charges, None
    return profile.charges, profile.charged_at + rules.CHARGE_INTERVAL


@transaction.atomic
def paint(user, board, x, y, color, free=False):
    """Закрасить клетку — хоть пустую, хоть чужую. Цвет выбирает сам человек.

    free — режим художника: модератор кладёт цвет без заряда, этим на старте доски
    задают первые рисунки. Всем остальным мазок стоит заряда.

    Пока включён rules.OWN_COLOR_ONLY, присланный цвет не в счёт: за аккаунтом
    закреплён свой, его и кладём.
    """
    profile = _ready(user, board, x, y)
    _check_color(color)
    if rules.OWN_COLOR_ONLY and not free:
        color = profile.color
    if free:
        require_moderator(user)
    else:
        _take_charge(profile)
    return _record(board, x, y, color, user)


@transaction.atomic
def erase(user, board, x, y):
    """Стереть свой пиксель. Бесплатно: ошибка кисти не должна стоить заряда.

    Чужой стереть нельзя — его закрашивают. Так у любой порчи остаётся цвет автора,
    а на опустевшей клетке всё равно виден тот, кто её опустошил (журнал пишем и тут).
    Модератору чужое стирать можно: без этого непотребство пришлось бы закрашивать.
    """
    _ready(user, board, x, y)
    pixel = Pixel.objects.select_for_update().filter(board=board, x=x, y=y).exclude(color=palette.EMPTY).first()
    if pixel is None:
        raise WallError("клетка и так пустая")
    if pixel.user_id != user.pk and not user.has_perm(MODERATOR):
        raise WallError("стереть можно только свой пиксель, чужой закрашивают")
    return _record(board, x, y, palette.EMPTY, user)


@transaction.atomic
def reroll(user):
    """Сменить закреплённый цвет за валюту. Новый всегда отличается от прежнего.

    Смысл имеет только при rules.OWN_COLOR_ONLY: когда палитра открыта всем, менять
    нечего. Вьюха это и проверяет.
    """
    profile_of(user)
    profile = WallProfile.objects.select_for_update().get(user=user)
    was = palette.get(profile.color)
    spend(user, rules.REROLL_PRICE, BalanceLog.Reason.WALL_REROLL, note=f"с «{was.name}»")
    profile.color = palette.roll(exclude=profile.color)
    profile.rerolls += 1
    profile.save(update_fields=["color", "rerolls"])
    return profile.color


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
    """Вернуть прямоугольник к состоянию на указанный момент.

    Ради этого журнал и ведётся. Для испорченного рисунка это правильнее ручной
    затирки: под мазком грифера восстанавливается то, что там было, а не дыра.
    """
    require_moderator(user)
    x1, y1, x2, y2 = _area(board, area)
    was = {}
    # Один проход по журналу вместо запроса на клетку: у прямоугольника в тысячу
    # клеток это тысяча запросов против одного.
    events = (
        board.placements
        .filter(x__range=(x1, x2), y__range=(y1, y2), created__lte=moment)
        .order_by("id").values_list("x", "y", "color")
    )
    for x, y, color in events.iterator():
        was[(x, y)] = color
    cells = {
        (x, y): was.get((x, y), palette.EMPTY)
        for y in range(y1, y2 + 1) for x in range(x1, x2 + 1)
    }
    return _write_many(board, cells, user)


@transaction.atomic
def stamp(user, board, cells):
    """Положить готовую картинку разом: {(x, y): цвет}.

    Инструмент консоли, а не доски: ни зарядов, ни потолка на площадь тут нет — стартовый
    арт раскладывать порциями по четыре тысячи клеток бессмысленно. Права всё равно
    спрашиваем: чужой цвет кладёт только модератор, откуда бы он ни пришёл.
    """
    require_moderator(user)
    for (x, y), color in cells.items():
        if not board.holds(x, y):
            raise WallError(f"клетка ({x}, {y}) вне доски")
        _check_color(color, empty_too=True)
    return _write_many(board, cells, user)


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


def ban(user, target, days):
    """Закрыть человеку доску, не трогая остальной сайт. days=0 — снять запрет."""
    require_moderator(user)
    profile = profile_of(target)
    profile.banned_until = timezone.now() + timedelta(days=days) if days else None
    profile.save(update_fields=["banned_until"])
    return profile


# --- чтение ---


def snapshot(board):
    """Доска одним куском: по байту на клетку, слева направо и сверху вниз."""
    buffer = bytearray(board.width * board.height)
    for x, y, color in board.pixels.exclude(color=palette.EMPTY).values_list("x", "y", "color"):
        buffer[y * board.width + x] = color
    return bytes(buffer)


def version(board):
    """Номер последнего события: им клиент догоняет пропущенное после обрыва связи."""
    return board.placements.aggregate(last=Max("id"))["last"] or 0


# Время в таймлапсе нужно только на подпись под ползунком, поэтому берём его у каждого
# N-го события, а не у каждого: на семестр это пара тысяч чисел вместо полумиллиона.
MARK_EVERY = 256


def journal(board):
    """Весь журнал доски: отметки времени и события подряд, по три байта на событие.

    Доска на любой момент — это первые N событий, наложенные на пустое полотно; ровно
    так её и собирает браузер, перематывая таймлапс. Отдельного «состояния на момент T»
    на сервере нет и не нужно: пробежать пятнадцать тысяч байтовых записей в памяти
    дешевле, чем один раз сходить за ними в базу.
    """
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
    """Кто трогал клетку, новое сверху. Со стираниями — иначе мстить будет некому."""
    return board.placements.filter(x=x, y=y).select_related("user")[:limit]


def require_moderator(user):
    """Публичная: вьюхам она нужна до того, как они пойдут искать чужие записи."""
    if not user.has_perm(MODERATOR):
        raise WallError("нужны права модератора Стены")


def require_admin(user):
    """Строже, чем модератор. Модератор правит рисунки, а закрыть доску — это конец
    сезона сразу для всех, такое доверяем только сотрудникам сайта."""
    if not user.is_staff:
        raise WallError("новую доску открывает только администратор")


@transaction.atomic
def open_board(user, title, width=None, height=None):
    """Закрыть нынешнюю доску и открыть новую, пустую.

    Старая никуда не девается: и рисунок, и журнал остаются в базе, просто со страницы
    её сменяет чистая. Поэтому «сбросить историю» — это не удаление, а начало нового
    сезона, к прошлому всегда можно вернуться.
    """
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
    # Размер по умолчанию наследуем у прошлой доски: сезон меняется, привычка нет.
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


def _area(board, area):
    """Прямоугольник из четырёх чисел: углы приводим к порядку, размер ограничиваем."""
    x1, x2 = sorted((area[0], area[2]))
    y1, y2 = sorted((area[1], area[3]))
    if not board.holds(x1, y1) or not board.holds(x2, y2):
        raise WallError("область выходит за доску")
    if (x2 - x1 + 1) * (y2 - y1 + 1) > rules.MAX_AREA:
        raise WallError(f"за раз можно взять не больше {rules.MAX_AREA} клеток")
    return x1, y1, x2, y2


def _ready(user, board, x, y):
    """Общие проверки и запертый профиль: дальше его можно менять без гонок."""
    if not board.is_active:
        raise WallError("доска закрыта")
    if not board.holds(x, y):
        raise WallError("клетка вне доски")
    profile_of(user)
    profile = WallProfile.objects.select_for_update().get(user=user)
    if profile.banned_until and profile.banned_until > timezone.now():
        raise WallError("доска для тебя закрыта")
    if not user.has_perm(MODERATOR) and board.areas.filter(
        x1__lte=x, x2__gte=x, y1__lte=y, y2__gte=y,
    ).exists():
        raise WallError("этот участок закрыт от правок")
    return profile


def _settle(profile, now):
    """Догнать заряды до текущего момента. Не сохраняет."""
    if profile.charges >= rules.MAX_CHARGES:
        profile.charged_at = now
        return
    gained = (now - profile.charged_at) // rules.CHARGE_INTERVAL
    if gained <= 0:
        return
    profile.charges = min(rules.MAX_CHARGES, profile.charges + gained)
    if profile.charges >= rules.MAX_CHARGES:
        profile.charged_at = now  # копилка полна, обрывки времени копить незачем
    else:
        # Именно сдвиг, а не «сейчас»: недокапавшийся заряд не должен обнуляться.
        profile.charged_at += gained * rules.CHARGE_INTERVAL


def _take_charge(profile):
    _settle(profile, timezone.now())
    if profile.charges < 1:
        raise NoCharges("пиксели кончились")
    profile.charges -= 1
    profile.save(update_fields=["charges", "charged_at"])


def _record(board, x, y, color, user):
    now = timezone.now()
    Pixel.objects.update_or_create(
        board=board, x=x, y=y, defaults={"color": color, "user": user, "placed": now},
    )
    placement = Placement.objects.create(board=board, x=x, y=y, color=color, user=user, created=now)
    # Рассылка после фиксации: иначе откат транзакции оставил бы у всех на полотне
    # пиксель, которого в базе нет. Заодно рассылают все, кто пишет через _record.
    transaction.on_commit(lambda: notify_pixel(placement))
    return placement


def _write_many(board, cells, user):
    """Пачка клеток одним заходом: два запроса на всё и одно событие в сокет.

    Поштучно вышло бы столько запросов и столько сообщений, сколько клеток, — заливка
    прямоугольника легла бы и на базу, и на каждого, кто в этот момент смотрит на доску.

    Порядок клеток сохраняем: в нём они лягут в журнал, а журнал — это таймлапс.
    """
    if not cells:
        return []
    now = timezone.now()
    keys = list(cells)
    current = {
        (x, y): color
        for x, y, color in board.pixels.filter(
            x__range=(min(x for x, _ in keys), max(x for x, _ in keys)),
            y__range=(min(y for _, y in keys), max(y for _, y in keys)),
        ).values_list("x", "y", "color")
    }
    # Клетки, где и так нужный цвет, не трогаем: незачем засорять ими журнал и историю.
    changed = {at: color for at, color in cells.items() if current.get(at, palette.EMPTY) != color}
    if not changed:
        return []

    Pixel.objects.bulk_create(
        [Pixel(board=board, x=x, y=y, color=color, user=user, placed=now) for (x, y), color in changed.items()],
        update_conflicts=True, update_fields=["color", "user", "placed"], unique_fields=["board", "x", "y"],
    )
    placements = Placement.objects.bulk_create(
        [Placement(board=board, x=x, y=y, color=color, user=user, created=now) for (x, y), color in changed.items()]
    )
    transaction.on_commit(lambda: notify_area(board.pk, placements))
    return placements
