"""Стена: страница доски, снимок полотна, постановка пикселей.

Здесь данные ходят JSON'ом, а не фрагментами разметки, как в остальном сайте. Причина
не в моде: у пикселя разметки нет — на полотне менять нечего, есть координата и цвет.
Фрагментом отдаётся только карточка клетки: она как раз разметка, и рисовать её
руками в JS было бы вдвое дороже.
"""

import struct
from datetime import timedelta

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from economy.services import NotEnoughFunds, wallet_of
from users.models import User

from . import palette, rules
from .models import Board, Pixel
from .services import (
    MARK_EVERY,
    MODERATOR,
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
    require_moderator,
    reroll,
    rollback,
    snapshot,
    status,
    unprotect,
    version,
)


def wall(request):
    board = _board()
    profile = profile_of(request.user)
    balance = wallet_of(request.user).balance
    return render(request, "wall/wall.html", {
        "board": board,
        "color": palette.get(profile.color),
        "balance": balance,
        "max_charges": rules.MAX_CHARGES,
        "price": rules.REROLL_PRICE,
        "own_color": rules.OWN_COLOR_ONLY,
        "data": {
            "board": board.pk,  # ключ для подложки в памяти браузера: у каждой доски своя
            "title": board.title,
            "width": board.width,
            "height": board.height,
            # Цветная часть палитры ложится сеткой: тон — колонка, светлота — строка.
            "hues": len(palette.HUES),
            # С этого места в палитре идут нейтральные — их показываем отдельной группой.
            "neutral_from": len(palette.PALETTE) - len(palette.NEUTRALS),
            # Индекс в списке — это и есть код цвета в снимке, нулевой означает пустую клетку.
            "colors": [{"hex": color.hex, "name": color.name} for color in palette.PALETTE],
            "max_charges": rules.MAX_CHARGES,
            "interval": rules.CHARGE_INTERVAL.total_seconds(),
            "price": rules.REROLL_PRICE,
            "balance": balance,
            "own_color": rules.OWN_COLOR_ONLY,
            "max_area": rules.MAX_AREA,
            "areas": _areas(board),
            "urls": {
                "snapshot": reverse("wall_snapshot"),
                "paint": reverse("wall_paint"),
                "erase": reverse("wall_erase"),
                "reroll": reverse("wall_reroll"),
                "pixel": reverse("wall_pixel"),
                "history": reverse("wall_history"),
                "fill": reverse("wall_fill"),
                "rollback": reverse("wall_rollback"),
                "protect": reverse("wall_protect"),
                "unprotect": reverse("wall_unprotect"),
                "ban": reverse("wall_ban"),
                "newboard": reverse("wall_board_new"),
            },
            **_state(profile),
        },
    })


def board_snapshot(request):
    """Полотно одним куском, по байту на клетку.

    На нашей доске это 9 КБ, после сжатия почти ничего, поэтому кэша тут нет: запрос
    к базе дешевле, чем согласовывать копии между воркерами.
    """
    board = _board()
    # Версия — номер последнего события. По ней клиент отбрасывает события, которые
    # старше снимка, иначе догнавший его пиксель откатил бы полотно назад.
    # Читаем её ДО полотна: наоборот мазок, легший между двумя запросами, попал бы
    # в номер, но не в снимок, и клиент отбросил бы его как уже учтённый.
    at = version(board)
    response = HttpResponse(snapshot(board), content_type="application/octet-stream")
    response["X-Wall-Version"] = at
    response["Cache-Control"] = "no-store"
    return response


def board_history(request):
    """Журнал доски для таймлапса: отметки времени, следом события подряд.

    Сначала отметки — по четыре байта, секунды от первого события; затем сами события,
    по три байта (x, y, цвет). Байты младшим вперёд: так их и читает Uint32Array
    в браузере, без разбора по одному.
    """
    board = _board()
    start, marks, events = journal(board)
    body = struct.pack(f"<{len(marks)}I", *marks) + events
    response = HttpResponse(body, content_type="application/octet-stream")
    response["X-Wall-Start"] = start.isoformat() if start else ""
    response["X-Wall-Marks"] = len(marks)
    response["X-Wall-Step"] = MARK_EVERY
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def pixel_paint(request):
    # free — режим художника, без заряда; право на него проверяет сервис.
    color = _number(request.POST, "color", palette.EMPTY)
    free = request.POST.get("free") == "1"
    return _place(request, lambda user, board, x, y: paint(user, board, x, y, color, free))


@require_POST
def pixel_erase(request):
    return _place(request, erase)


@require_POST
def color_reroll(request):
    if not rules.OWN_COLOR_ONLY:
        raise Http404("цвет за аккаунтом сейчас не закреплён")
    try:
        code = reroll(request.user)
    except NotEnoughFunds as error:
        return JsonResponse({"error": str(error)}, status=409)
    color = palette.get(code)
    return JsonResponse({
        "color": code, "hex": color.hex, "name": color.name,
        "balance": wallet_of(request.user).balance,
    })


@require_POST
def board_new(request):
    """Закрыть нынешнюю доску и открыть чистую. Прошлая уходит в архив целиком."""
    try:
        board = open_board(request.user, request.POST.get("title", ""))
    except WallError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({"board": board.pk, "title": board.title})


@require_POST
def area_fill(request):
    """Заливка прямоугольника. Пустой цвет очищает — отдельной кнопки не надо."""
    return _area_action(request, lambda board, area: fill(
        request.user, board, area, _number(request.POST, "color", palette.EMPTY),
    ))


@require_POST
def area_rollback(request):
    # Глубину зажимаем: из формы приходит что угодно, а timedelta от миллиарда часов
    # это уже не отказ, а пятисотка.
    hours = min(max(_number(request.POST, "hours", 1), 1), 24 * 365)
    return _area_action(request, lambda board, area: rollback(
        request.user, board, area, timezone.now() - timedelta(hours=hours),
    ))


@require_POST
def area_protect(request):
    board = _board()
    try:
        area = protect(request.user, board, _rect(request.POST), request.POST.get("note", ""))
    except WallError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({"areas": _areas(board), "added": area.pk})


@require_POST
def area_unprotect(request):
    board = _board()
    try:
        unprotect(request.user, board, _number(request.POST, "pk", 0))
    except WallError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({"areas": _areas(board)})


@require_POST
def person_ban(request):
    """Закрыть доску автору выбранного пикселя, не трогая остальной сайт."""
    try:
        # Права проверяем до поиска: иначе посторонний по кривому id получал бы 404
        # вместо отказа — и заодно узнавал бы, какие id в базе есть.
        require_moderator(request.user)
        target = User.objects.filter(pk=_number(request.POST, "user", 0)).first()
        if target is None:
            raise WallError("человек не найден")
        days = min(max(_number(request.POST, "days", 0), 0), 365)
        profile = ban(request.user, target, days)
    except WallError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({
        "until": profile.banned_until.isoformat() if profile.banned_until else None,
        "who": f"{target.name} {target.surname}",
    })


def pixel_card(request):
    """Кто трогал клетку. Стирания тоже здесь — на пустой клетке иначе некого спросить."""
    board = _board()
    try:
        x, y = _coords(request.GET)
    except WallError as error:
        raise Http404(error)
    if not board.holds(x, y):
        raise Http404("клетка вне доски")
    events = list(history(board, x, y))
    for event in events:
        event.shade = palette.get(event.color)
    now = (
        Pixel.objects.filter(board=board, x=x, y=y)
        .exclude(color=palette.EMPTY).select_related("user").first()
    )
    return render(request, "wall/_pixel.html", {
        "x": x, "y": y, "events": events,
        # Стирать дают только своё, и знает об этом сервер — кнопку включаем отсюда.
        # Модератору стирать можно любое, поэтому ему кнопка доступна всегда.
        "mine": now is not None and (now.user_id == request.user.pk or request.user.has_perm(MODERATOR)),
        "owner": now.user if now else None,
    })


def _board():
    board = Board.current()
    if board is None:
        raise Http404("доска ещё не открыта")
    return board


def _number(source, name, default):
    """Число из запроса. Мусор — это значение по умолчанию, а не 500."""
    try:
        return int(source[name])
    except (KeyError, TypeError, ValueError):
        return default


def _coords(source):
    """Координаты из запроса. Мусор — это отказ, а не 500."""
    try:
        return int(source["x"]), int(source["y"])
    except (KeyError, ValueError):
        raise WallError("непонятная клетка")


def _rect(source):
    try:
        return tuple(int(source[name]) for name in ("x1", "y1", "x2", "y2"))
    except (KeyError, ValueError):
        raise WallError("непонятная область")


def _areas(board):
    return list(board.areas.values("pk", "x1", "y1", "x2", "y2", "note"))


def _area_action(request, run):
    board = _board()
    try:
        placements = run(board, _rect(request.POST))
    except WallError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse({"changed": len(placements)})


def _state(profile, **extra):
    """Что клиенту нужно знать о себе после любого действия — и после отказа тоже,
    иначе таймер зарядов на странице разъедется с сервером."""
    charges, next_at = status(profile)
    return {
        "color": profile.color,
        "charges": charges,
        "next": next_at.isoformat() if next_at else None,
        **extra,
    }


def _place(request, action):
    board = _board()
    try:
        action(request.user, board, *_coords(request.POST))
    except NoCharges as error:
        return JsonResponse(_state(profile_of(request.user), error=str(error)), status=409)
    except WallError as error:
        return JsonResponse(_state(profile_of(request.user), error=str(error)), status=400)
    return JsonResponse(_state(profile_of(request.user)))
