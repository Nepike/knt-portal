"""«Сводка» Пульса: цифры периода из живых таблиц. Ночного снимка нет намеренно —
график по дням не стоит отдельной таблицы, а всё остальное считается на лету."""

from datetime import timedelta

from django.db.models import Count, Max, Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from comments.models import Comment
from core.models import Failure, Lockdown, Moderated
from core.templatetags.text_extras import plural
from economy.models import BalanceLog
from intake.models import MediaJob
from lectorium.models import Playlist
from library.models import Book
from materials.models import Material
from moderation.queue import GROUPS
from printing.models import Printer, PrintJob
from printing.pricing import rubles
from teachers.models import Review
from users.models import LoginEvent, User, UserSession

PERIODS = {"day": "Сегодня", "week": "7 дней", "month": "30 дней"}
DEFAULT = "week"
# Столько молчания — и человек уже не «на сайте». Отметка активности освежается раз
# в пять минут (users/sessions.py), меньше брать бессмысленно.
ONLINE = timedelta(minutes=15)
# За столько дней до срока оплаты он попадает в «Требует внимания».
DEADLINE_SOON = 14


def since(period, now=None):
    now = now or timezone.now()
    if period == "day":
        return timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(days=7 if period == "week" else 30)


def _visited(edge):
    """Заходившие с edge: по последнему входу и по отметке активности сессий. Одного
    входа мало — сессия живёт две недели, и человек ходит по сайту, не входя заново."""
    return User.objects.filter(is_active=True).filter(Q(last_login__gte=edge) | Q(sessions__seen__gte=edge)).distinct()


def tiles(edge):
    people = User.objects.filter(is_active=True).count()
    logins = LoginEvent.objects.filter(created__gte=edge).aggregate(
        ok=Count("id", filter=Q(kind=LoginEvent.Kind.LOGIN)),
        bad=Count("id", filter=Q(kind__in=[LoginEvent.Kind.FAILED, LoginEvent.Kind.STOPPED])),
    )
    first = LoginEvent.objects.order_by("created").values_list("created", flat=True).first()
    if first is None:
        logins_note = "журнал входов пока пуст"
    elif first > edge:
        logins_note = f"журнал ведётся с {date_format(timezone.localtime(first), 'j E')}"
    else:
        logins_note = f"и {logins['bad']} {plural(logins['bad'], 'неудачная попытка,неудачные попытки,неудачных попыток')}"

    works = [model.objects.filter(created__gte=edge).count() for model in (Material, Book, Playlist)]
    reviews = Review.objects.filter(created__gte=edge).count()
    said = Comment.objects.filter(created__gte=edge).count()
    money = BalanceLog.objects.filter(created__gte=edge).aggregate(
        n=Count("id"), plus=Sum("amount", filter=Q(amount__gt=0)), minus=Sum("amount", filter=Q(amount__lt=0)),
    )
    printed = PrintJob.objects.filter(submitted_at__gte=edge).aggregate(n=Count("id"), sides=Sum("sides"), cost=Sum("cost"))
    feed = reverse("pulse_feed")
    return [
        {"label": "Заходили", "value": _visited(edge).count(), "note": f"из {people} действующих",
         "href": reverse("pulse_people")},
        {"label": "Входы", "value": logins["ok"], "note": logins_note, "href": f"{feed}?kind=login"},
        {"label": "Новое в библиотеке", "value": sum(works),
         "note": f"материалы {works[0]} · книги {works[1]} · курсы {works[2]}", "href": f"{feed}?kind=upload"},
        {"label": "Отзывы и комментарии", "value": reviews + said, "note": f"отзывы {reviews} · комментарии {said}",
         "href": f"{feed}?kind=review"},
        {"label": "Токены", "value": money["n"],
         "note": f"{plural(money['n'], 'операция,операции,операций')} · +{money['plus'] or 0} / {money['minus'] or 0}",
         "href": f"{feed}?kind=wallet"},
        {"label": "Печать", "value": printed["n"],
         "note": f"{printed['sides'] or 0} стр. · {rubles(printed['cost'] or 0)}", "href": f"{feed}?kind=print"},
    ]


def visits(now=None):
    """«Кто заходит»: сегодня, неделя, месяц и раскладка всех действующих на три доли."""
    now = now or timezone.now()
    total = User.objects.filter(is_active=True).count()
    month = _visited(since("month", now)).count()
    never = User.objects.filter(is_active=True, last_login__isnull=True, sessions__isnull=True).count()
    away = max(total - month - never, 0)
    share = (lambda n: round(n * 100 / total, 1)) if total else (lambda n: 0)
    return {
        "today": _visited(since("day", now)).count(),
        "week": _visited(since("week", now)).count(),
        "month": month,
        "away": away,
        "never": never,
        "total": total,
        "month_share": share(month),
        "away_share": share(away),
    }


def attention(edge, now=None):
    """Что требует внимания. Тон: ok, warn, bad — как у принтера (Printer.condition)."""
    now = now or timezone.now()
    items = []

    lock = Lockdown.load()
    if lock.active:
        items.append({"tone": "bad", "text": "Сайт закрыт заслоном", "state": "открыть — в админке",
                      "href": reverse("admin:core_lockdown_changelist")})
    elif lock.deadline and lock.days_left <= DEADLINE_SOON:
        left = lock.days_left
        state = "срок прошёл" if left < 0 else ("сегодня последний день" if left == 0 else
                                                 f"через {left} {plural(left, 'день,дня,дней')}")
        items.append({"tone": "warn", "text": f"Оплата: срок {date_format(lock.deadline, 'j E')}", "state": state,
                      "href": reverse("admin:core_lockdown_changelist")})

    waiting = sum(model.objects.filter(status=Moderated.Status.PENDING).count() for _, model, *_ in GROUPS)
    items.append({"tone": "warn" if waiting else "ok", "text": "На модерации",
                  "state": f"{waiting} {plural(waiting, 'работа,работы,работ')}" if waiting else "очередь пуста",
                  "href": reverse("review_queue")})

    for printer in Printer.objects.filter(active=True):
        text, tone = printer.condition()
        toner = f" · тонер {printer.toner_percent}%" if printer.toner_percent is not None else ""
        items.append({"tone": tone, "text": printer.name, "state": text + toner,
                      "href": reverse("admin:printing_printer_change", args=[printer.pk])})

    jobs = MediaJob.objects.aggregate(
        queued=Count("id", filter=Q(status__in=[MediaJob.Status.WAITING, MediaJob.Status.BAKING])),
        failed=Count("id", filter=Q(status=MediaJob.Status.FAILED, updated__gte=edge)),
    )
    state = f"в очереди {jobs['queued']}" if jobs["queued"] else "очередь пуста"
    if jobs["failed"]:
        state += f" · не вышло {jobs['failed']}"
    items.append({"tone": "warn" if jobs["failed"] else "ok", "text": "Пекарня", "state": state,
                  "href": reverse("admin:intake_mediajob_changelist")})

    failures = dict(
        Failure.objects.filter(last_at__gte=edge).values_list("source").annotate(n=Sum("times")).order_by()
    )
    names = dict(Failure.Source.choices)
    if failures:
        state = " · ".join(f"{names[source]} {n}" for source, n in sorted(failures.items()))
        items.append({"tone": "bad", "text": "Сбои", "state": state, "href": reverse("admin:core_failure_changelist")})
    else:
        items.append({"tone": "ok", "text": "Сбои", "state": "не было", "href": reverse("admin:core_failure_changelist")})
    return items


def online(now=None):
    """Кто на сайте сейчас, свежие сверху: (человек, «был 3 мин назад»)."""
    now = now or timezone.now()
    rows = list(
        UserSession.objects.filter(seen__gte=now - ONLINE, user__is_active=True)
        .values("user").annotate(seen=Max("seen")).order_by("-seen")
    )
    people = User.objects.in_bulk([row["user"] for row in rows])
    result = []
    for row in rows:
        minutes = int((now - row["seen"]).total_seconds() // 60)
        result.append((people[row["user"]], "только что" if minutes < 1 else f"{minutes} мин назад"))
    return result
