"""Стена: страница доски и её данные. Всё, кроме страницы, — JSON или байты: у клетки нет разметки."""

import base64
import json
import struct
from datetime import timedelta

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.throttle import throttled
from users.models import User

from . import palette, rules
from .models import Board, Pixel
from .services import (
    MARK_EVERY,
    MODERATOR,
    SKIP,
    STAMPER,
    WallError,
    ban,
    bans,
    draw,
    fill,
    fit,
    history,
    journal,
    open_board,
    owners,
    profile_of,
    protect,
    require_moderator,
    rollback,
    rollback_person,
    snapshot,
    stamp,
    undo,
    unprotect,
    version,
)

BAN_LABELS = {1: "1 час", 24: "1 день", 24 * 7: "7 дней", 24 * 30: "30 дней", -1: "навсегда"}
FITS_PER_WINDOW = 120  # подгонок подложки за 10 минут: каждая — подбор цвета на сервере


def wall(request):
    board = _board()
    profile = profile_of(request.user)
    user = request.user
    return render(request, "wall/wall.html", {
        "board": board,
        "data": {
            "board": board.pk,  # ключ подложки и кисти в памяти браузера: у каждой доски свои
            "title": board.title,
            "width": board.width,
            "height": board.height,
            # Индекс в списке — код цвета в снимке, нулевой — пустая клетка.
            "colors": [{"hex": color.hex, "name": color.name} for color in palette.PALETTE],
            "hues": len(palette.HUES),
            "tones": len(palette.TONES),
            "neutral_from": palette.NEUTRAL_FROM,
            "bright_from": palette.BRIGHT_FROM,
            "stroke_max": rules.STROKE_MAX,
            "max_area": rules.MAX_AREA,
            "areas": _areas(board),
            "me": user.pk,
            "me_name": user.full_name,
            "can": {
                "moderate": user.has_perm(MODERATOR),
                "stamp": user.has_perm(STAMPER) or user.has_perm(MODERATOR),
                "admin": user.is_staff,
            },
            "ban_hours": [[hours, label] for hours, label in BAN_LABELS.items()],
            "banned": _ban_state(profile),
            "urls": {
                name: reverse(f"wall_{name}") for name in (
                    "snapshot", "owners", "history", "draw", "undo", "pixel", "fit", "stamp",
                    "fill", "rollback", "rollback_person", "protect", "unprotect", "ban", "bans", "new",
                )
            },
        },
    })


def board_snapshot(request):
    """Полотно одним куском, по байту на клетку. Версию читаем ДО полотна: иначе мазок между
    запросами попал бы в номер, но не в снимок."""
    board = _board()
    at = version(board)
    response = HttpResponse(snapshot(board), content_type="application/octet-stream")
    response["X-Wall-Version"] = at
    response["Cache-Control"] = "no-store"
    return response


def board_owners(request):
    """Кто и когда поставил каждую клетку — подсказка «кто» без запроса на каждое наведение.
    who — Uint16 (номер в people с единицы), when — Uint32 (секунды от epoch), младшим вперёд."""
    board = _board()
    at = version(board)
    people, who, when = owners(board)
    return JsonResponse({
        "version": at,
        "epoch": board.created.isoformat(),
        "people": people,
        "who": base64.b64encode(struct.pack(f"<{len(who)}H", *who)).decode(),
        "when": base64.b64encode(struct.pack(f"<{len(when)}I", *when)).decode(),
    }, headers={"Cache-Control": "no-store"})


def board_history(request):
    """Журнал для таймлапса: отметки (по 4 байта, секунды от первого события), затем события
    по 3 байта (x, y, цвет). Младшим вперёд — так читает Uint32Array."""
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
def board_draw(request):
    """Мазок кистью или ластиком: {"cells": [[x, y, цвет], …]}. В ответ — id событий (для
    отмены) и клетки, которые не дали тронуть, с причиной."""
    board = _board()
    try:
        cells = [(int(x), int(y), int(color)) for x, y, color in _json(request)["cells"]]
        placements, rejected = draw(request.user, board, cells)
    except (KeyError, TypeError, ValueError):
        return _refuse("непонятный мазок")
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({
        "ids": [placement.pk for placement in placements],
        "rejected": [[x, y] for x, y in rejected],
        "reason": next(iter(rejected.values()), ""),
    })


@require_POST
def board_undo(request):
    """Отменить свои события: {"ids": […]}. Клетки приедут по сокету — с прежними авторами."""
    board = _board()
    try:
        ids = [int(pk) for pk in _json(request)["ids"]]
        placements = undo(request.user, board, ids)
    except (KeyError, TypeError, ValueError):
        return _refuse("непонятная отмена")
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({"changed": len(placements)})


def pixel_card(request):
    """Кто менял клетку — новое сверху, со стираниями."""
    board = _board()
    try:
        x, y = int(request.GET["x"]), int(request.GET["y"])
    except (KeyError, ValueError):
        raise Http404("непонятная клетка")
    if not board.holds(x, y):
        raise Http404("клетка вне доски")
    now = Pixel.objects.filter(board=board, x=x, y=y).select_related("user").first()
    return JsonResponse({
        "x": x, "y": y,
        "owner": _person(now.user) if now and now.user else None,
        "color": now.color if now else palette.EMPTY,
        "events": [
            {"who": _person(event.user), "color": event.color, "at": event.created.isoformat()}
            for event in history(board, x, y)
        ],
    })


@require_POST
def picture_fit(request):
    """Подложка или штамп в цветах палитры: {"w", "h", "rgba": base64} → коды по байту на клетку
    (255 — прозрачное). Подбор цвета — один на сайт и консоль (services.fit)."""
    if throttled(f"wall:fit:{request.user.pk}", FITS_PER_WINDOW, 600):
        return _refuse("слишком часто — подожди пару минут", status=429)
    try:
        data = _json(request)
        codes = fit(int(data["w"]), int(data["h"]), base64.b64decode(data["rgba"]))
    except (KeyError, TypeError, ValueError):
        return _refuse("непонятная картинка")
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({"codes": base64.b64encode(codes).decode()})


@require_POST
def picture_stamp(request):
    """Нанести картинку: {"x", "y", "w", "h", "codes": base64, "only_empty"}. Право — stamp_wall."""
    board = _board()
    try:
        data = _json(request)
        left, top, width, height = (int(data[key]) for key in ("x", "y", "w", "h"))
        codes = base64.b64decode(data["codes"])
        if width <= 0 or height <= 0 or len(codes) != width * height:
            raise ValueError
        cells = {
            (left + index % width, top + index // width): code
            for index, code in enumerate(codes) if code != SKIP
        }
        placements = stamp(request.user, board, cells, only_empty=bool(data.get("only_empty")))
    except (KeyError, TypeError, ValueError):
        return _refuse("непонятный штамп")
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({"changed": len(placements)})


@require_POST
def area_fill(request):
    """Заливка прямоугольника. Пустой цвет очищает."""
    return _area_action(request, lambda board, area: fill(
        request.user, board, area, _number(request.POST, "color", palette.EMPTY),
    ))


@require_POST
def area_rollback(request):
    hours = _hours(request.POST)
    return _area_action(request, lambda board, area: rollback(
        request.user, board, area, timezone.now() - timedelta(hours=hours),
    ))


@require_POST
def person_rollback(request):
    """Убрать правки человека за последние часы."""
    board = _board()
    try:
        require_moderator(request.user)
        placements = rollback_person(
            request.user, board, _target(request.POST), timezone.now() - timedelta(hours=_hours(request.POST)),
        )
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({"changed": len(placements)})


@require_POST
def area_protect(request):
    board = _board()
    try:
        area = protect(request.user, board, _rect(request.POST), request.POST.get("note", ""))
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({"areas": _areas(board), "added": area.pk})


@require_POST
def area_unprotect(request):
    board = _board()
    try:
        unprotect(request.user, board, _number(request.POST, "pk", 0))
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({"areas": _areas(board)})


@require_POST
def person_ban(request):
    """Закрыть доску человеку на срок из BAN_LABELS (0 — снять), с причиной."""
    try:
        # Права — до поиска: иначе по кривому id посторонний узнавал бы, какие id есть.
        require_moderator(request.user)
        target = _target(request.POST)
        hours = _number(request.POST, "hours", 0)
        if hours and hours not in BAN_LABELS:
            raise WallError("такого срока нет")
        profile = ban(request.user, target, hours, request.POST.get("reason", ""))
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({"who": target.full_name, **_ban_state(profile)})


def person_bans(request):
    """Кому доска закрыта сейчас — для панели модератора."""
    try:
        require_moderator(request.user)
    except WallError as error:
        return _refuse(str(error), status=403)
    return JsonResponse({"bans": [
        {**_person(profile.user), **_ban_state(profile), "by": profile.banned_by.full_name if profile.banned_by else ""}
        for profile in bans()
    ]})


@require_POST
def board_new(request):
    """Закрыть нынешнюю доску и открыть чистую. Прошлая уходит в архив целиком."""
    try:
        board = open_board(request.user, request.POST.get("title", ""))
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({"board": board.pk, "title": board.title})


def _board():
    board = Board.current()
    if board is None:
        raise Http404("доска ещё не открыта")
    return board


def _json(request):
    return json.loads(request.body or b"{}")


def _refuse(text, status=400):
    return JsonResponse({"error": text}, status=status)


def _number(source, name, default):
    """Число из запроса. Мусор — значение по умолчанию, а не 500."""
    try:
        return int(source[name])
    except (KeyError, TypeError, ValueError):
        return default


def _hours(source):
    # Глубину зажимаем: timedelta от миллиарда часов — пятисотка.
    return min(max(_number(source, "hours", 1), 1), 24 * 365)


def _rect(source):
    try:
        return tuple(int(source[name]) for name in ("x1", "y1", "x2", "y2"))
    except (KeyError, ValueError):
        raise WallError("непонятная область")


def _target(source):
    target = User.objects.filter(pk=_number(source, "user", 0)).first()
    if target is None:
        raise WallError("человек не найден")
    return target


def _person(user):
    if user is None:
        return None
    return {"id": user.pk, "name": user.full_name, "url": reverse("profile", args=[user.pk])}


def _ban_state(profile):
    if not profile.banned:
        return {"until": None, "forever": False, "reason": ""}
    return {"until": profile.banned_until.isoformat(), "forever": profile.forever, "reason": profile.ban_reason}


def _areas(board):
    return list(board.areas.values("pk", "x1", "y1", "x2", "y2", "note"))


def _area_action(request, run):
    board = _board()
    try:
        placements = run(board, _rect(request.POST))
    except WallError as error:
        return _refuse(str(error))
    return JsonResponse({"changed": len(placements)})
