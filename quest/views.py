from django.contrib.auth.decorators import login_not_required
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from core.context_processors import GLITCH_FROM, ZERO_DAY, midnight, today, zero
from core.context_processors import party as party_day
from core.throttle import client_ip, throttled
from economy import rewards

from . import keys
from .models import Broadcast, Clue, Find, Fragment, Tally, Unlock, plain

# Сколько вопросов консоли сервер выдержит от одного человека за 10 минут. Сюда идёт каждая
# команда — и те, на которые браузер ответил сам: их только считают (Tally). Браузер не
# пускает чаще 8 команд за 20 с, то есть 240 за 10 минут. Гостей считаем по адресу, и
# в общежитии за одним адресом — многие: им запас больше.
ASKS_PER_USER = 240
ASKS_PER_ADDRESS = 900
WINDOW = 600
# Сколько команд одного человека (гостя — адреса) за сутки идёт в счёт праздничной
# страницы. Честно столько не набирают; скрипт без потолка накрутил бы что угодно.
COUNTED_PER_DAY = 300


def running():
    """Отсчёт есть только пока висит глитч: до и после него страницы нет вовсе —
    о событии на сайте заранее ничего не говорим."""
    return today()[0] == "glitch"


@login_not_required
@require_safe
def countdown(request):
    """Страница отсчёта — на время скина главная для всех, и для гостей (core.views.home).

    Дата цели приходит отсюда, а не из скрипта, и ни одна команда консоли её не меняет:
    консоль живёт в браузере. Вместе с ней — часы сервера: по ним браузер поправит свои,
    если они у человека врут.
    """
    if not running():
        raise Http404
    context = {
        "since": midnight(GLITCH_FROM),
        "deadline": midnight(ZERO_DAY),
        "now": int(timezone.now().timestamp() * 1000),
        "slots": keys.slots(request.user),
        "zero": zero(),
    }
    if context["zero"]:
        # Передача — только после нуля: раньше адрес записи не должен попасть в страницу.
        broadcast = Broadcast.current()
        context["broadcast"] = broadcast.lecture if broadcast and broadcast.lecture and broadcast.lecture.prefix else None
        context["unlocked"] = request.user.is_authenticated and Unlock.objects.filter(user=request.user).exists()
        context["done"] = sum(1 for slot in context["slots"] if slot["value"])
    return render(request, "quest/countdown.html", context)


@login_not_required
@require_POST
def ask(request):
    """Консоль отсчёта спрашивает про команду, на которую у неё своего ответа нет.

    Каталог key/ и сдача фрагментов (found) — quest.keys. Подсказки квеста — в базе
    (quest.Clue), поэтому и ответ на них — отсюда. Нет такой — `text: null`, и браузер
    пишет обычное «команда не найдена». Команду, на которую браузер ответил сам, он шлёт
    с пометкой `local` — только для счёта.
    """
    if not running():
        raise Http404
    who = f"user:{request.user.pk}" if request.user.is_authenticated else f"ip:{client_ip(request)}"
    limit = ASKS_PER_USER if request.user.is_authenticated else ASKS_PER_ADDRESS
    if throttled(f"quest:ask:{who}", limit, WINDOW):
        return JsonResponse({"text": "канал перегружен. подождите пару минут.", "warn": True}, status=429)
    if not throttled(f"quest:tally:{who}", COUNTED_PER_DAY, 86400):
        Tally.bump(Tally.COMMANDS)
    if request.POST.get("local"):
        return JsonResponse({"text": None})
    command = plain(request.POST.get("command", "")[:60])
    reply = keys.answer(request, command)
    if reply:
        return JsonResponse(reply)
    clue = Clue.objects.filter(place=Clue.Place.COMMAND, command=command, active=True).first() if command else None
    if clue is None:
        return JsonResponse({"text": None})
    return JsonResponse({"text": clue.text, "laugh": clue.laugh})


# Между ячейками ключа в «Твой взлом» — пробел (party.html, white-space: pre). Ячейки и так
# различаются цветом, а каждый лишний знак в строке мельчит шрифт.
KEY_GAP = " "


def grouped(n):
    """12345 → «12 345»: разряды через неразрывный пробел, как пишут по-русски."""
    return f"{n:,}".replace(",", " ")


@login_not_required
@require_safe
def party(request):
    """Праздник после взлома — 2 декабря, главная для всех, и для гостей (core.views.home).
    Только в этот день: до и после страницы нет.

    Числа настоящие и только числа: сколько людей вскрыли ключ, сколько фрагментов сдано,
    сколько команд набрали в консоли и сколько дней шёл взлом. Имён нашедших нет намеренно.
    Ещё — свой итог («Твой взлом»): ключ ячейками, как на экране отсчёта, и вскрыл ли его
    человек. Гостю ключ закрыт целиком, и карточка зовёт войти. Не было квеста (фрагментов
    нет) — нет и карточки.
    """
    if not party_day():
        raise Http404
    broadcast = Broadcast.current()
    numbers = [
        (Unlock.objects.count(), "нашли ключ"),
        (Find.objects.count(), "фрагментов сдано"),
        (Tally.of(Tally.COMMANDS), "команд в консоли"),
        ((ZERO_DAY - GLITCH_FROM).days, "дней взлома"),
    ]
    context = {
        "numbers": [(grouped(n), label) for n, label in numbers],
        "frame": broadcast.frame if broadcast else None,
    }
    if Fragment.objects.exists():
        slots = keys.slots(request.user)
        cells = [{"text": slot["value"] or keys.REDACTED, "open": bool(slot["value"])} for slot in slots]
        # Ключ стоит одной строкой: шрифт подгоняет css по ширине карточки и длине строки в
        # знаках (.party-key в theme/input.css), а длину знает только сервер.
        length = len(KEY_GAP.join(cell["text"] for cell in cells))
        context["mine"] = {"cells": cells, "length": length, "total": len(slots)}
        if request.user.is_authenticated:
            context["mine"].update(
                done=sum(1 for slot in slots if slot["value"]),
                unlocked=Unlock.objects.filter(user=request.user).exists(),
                reward=rewards.QUEST_KEY,
            )
    return render(request, "quest/party.html", context)
