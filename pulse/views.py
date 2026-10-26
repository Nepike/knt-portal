from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.formats import date_format
from django.views.decorators.http import require_POST

from core.models import Moderated
from cosmetics.models import Case, CosmeticItem
from core.search import by_name
from users.models import User
from wall.models import ProtectedArea

from . import actions, feed, people, summary, system, tokens, wallstats
from .access import pulse_only


@pulse_only
def home(request):
    period = request.GET.get("period")
    if period not in summary.PERIODS:
        period = summary.DEFAULT
    edge = summary.since(period)
    texts = feed.events(timezone.now(), kinds=["review", "comment"], limit=6)
    return render(request, "pulse/home.html", {
        "tab": "home",
        "period": period,
        "periods": summary.PERIODS,
        "tiles": summary.tiles(edge),
        "visits": summary.visits(),
        "attention": summary.attention(edge),
        "texts": actions.offered(texts, request.user),
        "online": summary.online(),
    })


def _day(moment, today):
    day = timezone.localtime(moment).date()
    label = date_format(day, "j E")
    if day == today:
        return f"Сегодня, {label}"
    if day == today - timedelta(days=1):
        return f"Вчера, {label}"
    return f"{label}, {date_format(day, 'l').lower()}"


@pulse_only
def feed_page(request):
    kind = request.GET.get("kind", "")
    query = request.GET.get("q", "").strip()
    person = None
    who = None
    if request.GET.get("person", "").isdigit():
        person = get_object_or_404(User, pk=request.GET["person"])
        who = [person.pk]
    elif query:
        who = list(by_name(User.objects.all(), query).values_list("pk", flat=True)[:50])
    cursor = parse_datetime(request.GET.get("before", ""))
    events = feed.events(cursor or timezone.now(), [kind] if kind in feed.KINDS else None, who)
    actions.offered(events, request.user)

    # День — заголовком над первой строкой дня. Предыдущий приезжает с курсором: иначе
    # порция, начатая посреди дня, повторила бы его заголовок.
    today, previous = timezone.localdate(), request.GET.get("day", "")
    for event in events:
        event.day = _day(event.when, today)
        event.new_day = event.day != previous
        previous = event.day
    more = ""
    if len(events) == feed.PAGE:
        keep = {key: value for key, value in (("kind", kind), ("q", query)) if value}
        if person:
            keep["person"] = person.pk
        more = urlencode({**keep, "before": events[-1].when.isoformat(), "day": previous})

    base = {key: value for key, value in (("q", query), ("person", person.pk if person else "")) if value}
    chips = [("", "Все")] + list(feed.KINDS.items())
    context = {
        "tab": "feed", "kind": kind if kind in feed.KINDS else "", "query": query, "person": person,
        "events": events, "more": more, "cursor": cursor,
        "chips": [(label, urlencode({**base, **({"kind": key} if key else {})}), key == kind) for key, label in chips],
        "everyone": urlencode({"kind": kind} if kind in feed.KINDS else {}),
    }
    if cursor and request.headers.get("HX-Request"):
        return render(request, "pulse/_feed_page.html", context)
    return render(request, "pulse/feed.html", context)


def _done(request, text):
    """Ответ на действие: htmx получает строку на месте карточки и тост, обычный
    запрос — возврат в Сводку с сообщением."""
    if request.headers.get("HX-Request"):
        return render(request, "pulse/_done.html", {"text": text})
    messages.success(request, text)
    return redirect("pulse_home")


@pulse_only
@require_POST
def remove(request, kind, pk):
    if kind not in actions.REMOVABLE:
        raise Http404
    model, perm, *_ = actions.REMOVABLE[kind]
    if not request.user.has_perm(perm):
        raise PermissionDenied
    actions.remove(request, get_object_or_404(model, pk=pk), kind)
    return _done(request, "Удалено")


@pulse_only
@require_POST
def unpublish(request, kind, pk):
    if kind not in actions.REVIEWABLE:
        raise Http404
    model, perm = actions.REVIEWABLE[kind]
    if not request.user.has_perm(perm):
        raise PermissionDenied
    work = get_object_or_404(model, pk=pk)
    if work.status != Moderated.Status.APPROVED:
        return _done(request, "Уже не опубликовано")
    actions.unpublish(request, work)
    return _done(request, "Возвращено на проверку")


@pulse_only
def people_page(request):
    query = request.GET.get("q", "").strip()
    preset = request.GET.get("only", "")
    if preset not in people.PRESETS:
        preset = ""
    page = Paginator(people.find(query, preset), people.PAGE).get_page(request.GET.get("page"))
    keep = urlencode({key: value for key, value in (("q", query), ("only", preset)) if value})
    return render(request, "pulse/people.html", {
        "tab": "people", "query": query, "preset": preset, "presets": people.PRESETS, "page": page, "keep": keep,
    })


@pulse_only
def person_page(request, pk):
    person = get_object_or_404(User.objects.select_related("team", "wallet"), pk=pk)
    return render(request, "pulse/person.html", {
        "tab": "people", "person": person, **people.card(person, request.user),
    })


def _act(request, pk, do):
    """Действие над человеком: результат или отказ — сообщением, и назад в карточку."""
    person = get_object_or_404(User, pk=pk)
    try:
        messages.success(request, do(person))
    except people.Refused as refusal:
        messages.error(request, str(refusal))
    return redirect("pulse_person", pk=pk)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@pulse_only
@require_POST
def person_printing(request, pk):
    return _act(request, pk, lambda person: people.ban_printing(request, person, request.POST.get("banned") == "1"))


@pulse_only
@require_POST
def person_wall(request, pk):
    days = _int(request.POST.get("days"))
    return _act(request, pk, lambda person: people.ban_wall(request, person, days))


@pulse_only
@require_POST
def person_active(request, pk):
    return _act(request, pk, lambda person: people.set_active(request, person, request.POST.get("active") == "1"))


@pulse_only
@require_POST
def person_right(request, pk):
    name, on = request.POST.get("group", ""), request.POST.get("on") == "1"
    return _act(request, pk, lambda person: people.set_right(request, person, name, on))


@pulse_only
@require_POST
def person_sessions(request, pk):
    session = _int(request.POST.get("id"))
    return _act(request, pk, lambda person: people.end_sessions(request, person, session))


@pulse_only
@require_POST
def person_tokens(request, pk):
    amount, note = abs(_int(request.POST.get("amount")) or 0), request.POST.get("note", "").strip()
    if request.POST.get("sign") == "-":
        amount = -amount
    return _act(request, pk, lambda person: people.grant(request, person, amount, note))


@pulse_only
@require_POST
def person_password(request, pk):
    how = request.POST.get("how", "")
    return _act(request, pk, lambda person: people.password(request, person, how))


@pulse_only
@require_POST
def person_session(request, pk):
    """Ключ уходит один раз — в ответе, а не в сообщениях: те лежат в сессии смотрящего."""
    person = get_object_or_404(User, pk=pk)
    minutes = _int(request.POST.get("minutes"))
    try:
        key = people.test_session(request, person, minutes)
    except people.Refused as refusal:
        return render(request, "pulse/_session.html", {"error": str(refusal)})
    response = render(request, "pulse/_session.html", {
        "key": key, "cookie": settings.SESSION_COOKIE_NAME, "secure": settings.SESSION_COOKIE_SECURE,
        "until": timezone.now() + timedelta(minutes=minutes),
    })
    response["Cache-Control"] = "no-store"
    return response


@pulse_only
@require_POST
def person_gift(request, pk):
    item = CosmeticItem.objects.filter(pk=_int(request.POST.get("item"))).first()
    return _act(request, pk, lambda person: people.gift(request, person, item))


def _period(request):
    period = request.GET.get("period")
    return period if period in summary.PERIODS else summary.DEFAULT


def _back(request, name, text=None, error=None):
    if text:
        messages.success(request, text)
    if error:
        messages.error(request, error)
    return redirect(name)


@pulse_only
def tokens_page(request):
    period = _period(request)
    return render(request, "pulse/tokens.html", {
        "tab": "tokens", "period": period, "periods": summary.PERIODS,
        **tokens.overview(summary.since(period)), "cases": tokens.cases(), "rarity": tokens.rarity(),
    })


@pulse_only
@require_POST
def case_toggle(request, pk):
    case = get_object_or_404(Case, pk=pk)
    try:
        return _back(request, "pulse_tokens", tokens.toggle_case(request, case, request.POST.get("active") == "1"))
    except people.Refused as refusal:
        return _back(request, "pulse_tokens", error=str(refusal))


@pulse_only
@require_POST
def reconcile(request):
    """Сверка кошельков: ответ — кусок с расхождениями на месте кнопки."""
    apply = request.POST.get("apply") == "1"
    try:
        broken = tokens.reconcile(request, apply)
    except people.Refused as refusal:
        return render(request, "pulse/_reconcile.html", {"error": str(refusal)})
    return render(request, "pulse/_reconcile.html", {"broken": broken, "applied": apply})


@pulse_only
def wall_page(request):
    return render(request, "pulse/wall.html", {"tab": "wall", **wallstats.overview()})


@pulse_only
@require_POST
def wall_unban(request, pk):
    person = get_object_or_404(User, pk=pk)
    try:
        return _back(request, "pulse_wall", wallstats.unban(request, person))
    except people.Refused as refusal:
        return _back(request, "pulse_wall", error=str(refusal))


@pulse_only
@require_POST
def wall_unprotect(request, pk):
    area = get_object_or_404(ProtectedArea, pk=pk)
    try:
        return _back(request, "pulse_wall", wallstats.unprotect(request, area))
    except people.Refused as refusal:
        return _back(request, "pulse_wall", error=str(refusal))


@pulse_only
def system_page(request):
    return render(request, "pulse/system.html", {
        "tab": "system", **system.services(), "schedule": system.schedule(), "failures": system.failures(),
    })


@pulse_only
@require_POST
def system_lockdown(request):
    try:
        return _back(request, "pulse_system", system.set_lockdown(request, request.POST.get("active") == "1"))
    except people.Refused as refusal:
        return _back(request, "pulse_system", error=str(refusal))


@pulse_only
@require_POST
def system_backup(request):
    try:
        return _back(request, "pulse_system", system.backup_now(request))
    except people.Refused as refusal:
        return _back(request, "pulse_system", error=str(refusal))


@pulse_only
def system_backups(request):
    return render(request, "pulse/_backups.html", {"copies": system.backups()})


@pulse_only
@require_POST
def system_queue(request):
    return render(request, "pulse/_queue.html", {"alive": system.queue_alive()})
