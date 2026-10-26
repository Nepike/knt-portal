"""«Стена» в Пульсе — то, что есть сейчас. Раздел Стены будут переделывать, и эта
вкладка поменяется вместе с ним (память pulse-admin): полировать её незачем.

Ходы по дням считаются из журнала самой Стены (Placement) — своей таблицы не нужно.
"""

from datetime import timedelta

from django.contrib.admin.models import DELETION
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from users.models import User
from wall import services as wall
from wall.models import Board, Pixel, Placement, ProtectedArea, WallProfile
from wall.palette import EMPTY

from . import feed
from .access import logged
from .people import Refused

DAYS = 30


def _top(moves, limit=5):
    rows = list(moves.values_list("user").annotate(n=Count("id")).order_by("-n")[:limit])
    people = User.objects.in_bulk([pk for pk, _ in rows])
    return [(people.get(pk), n) for pk, n in rows]


def overview(now=None):
    now = now or timezone.now()
    board = Board.objects.filter(is_active=True).order_by("-created").first()
    if board is None:
        return {"board": None}
    moves = Placement.objects.filter(board=board)
    week = moves.filter(created__gte=now - timedelta(days=7))
    start = timezone.localdate(now) - timedelta(days=DAYS - 1)
    counted = dict(
        moves.filter(created__date__gte=start).annotate(day=TruncDate("created"))
        .values_list("day").annotate(n=Count("id")).order_by()
    )
    days = [(start + timedelta(days=n), counted.get(start + timedelta(days=n), 0)) for n in range(DAYS)]
    peak = max((n for _, n in days), default=0) or 1
    return {
        "board": board,
        "cells": board.width * board.height,
        "painted": Pixel.objects.filter(board=board).exclude(color=EMPTY).count(),
        "moves": moves.count(),
        "erased": moves.filter(color=EMPTY).count(),
        "week": week.count(),
        "week_artists": week.values("user").distinct().count(),
        "artists": moves.values("user").distinct().count(),
        "days": [{"day": day, "n": n, "height": round(n * 100 / peak)} for day, n in days],
        "days_total": sum(n for _, n in days),
        "top_week": _top(week),
        "top_all": _top(moves),
        "recent": feed.wall(now, 10, None),
        "banned": list(WallProfile.objects.filter(banned_until__gt=now).select_related("user").order_by("banned_until")),
        "areas": list(ProtectedArea.objects.filter(board=board).select_related("by")),
        "past": list(Board.objects.filter(is_active=False).order_by("-closed", "-created")),
    }


def unban(request, person):
    try:
        wall.ban(request.user, person, 0)
    except wall.WallError as error:
        raise Refused(str(error).capitalize()) from error
    logged(request, person, "Стена открыта")
    return f"{person.full_name}: Стена открыта"


def unprotect(request, area):
    try:
        wall.require_moderator(request.user)
    except wall.WallError as error:
        raise Refused(str(error).capitalize()) from error
    logged(request, area, "защита области снята", DELETION)
    wall.unprotect(request.user, area.board, area.pk)
    return "Защита снята"
