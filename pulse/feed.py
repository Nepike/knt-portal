"""Лента Пульса: события из живых таблиц, слитые по времени.

Отдельной таблицы событий нет намеренно: её пришлось бы писать из каждого приложения,
и разойтись с самими данными она могла бы в первый же день. Здесь у каждого вида свой
источник — функция (до какого времени, сколько, чьи) → события, — а лента берёт из
каждого по порции и сливает. Курсор — время последнего показанного события.

Подписи — существительными («вход», «отзыв о преподавателе»), а не глаголами: пола у
человека на сайте нет, и «вошёл» про студентку было бы неправдой.
"""

from dataclasses import dataclass, field
from datetime import datetime

from django.db.models import Count, Max
from django.db.models.functions import TruncHour
from django.urls import reverse

from comments.models import Comment
from core.models import Moderated
from core.templatetags.text_extras import plural
from cosmetics.models import CaseOpening
from economy.models import BalanceLog
from lectorium.models import Playlist
from library.models import Book
from materials.models import Material
from printing.models import PrintJob
from printing.pricing import rubles
from teachers.models import Review
from users.models import LoginEvent, User
from wall.models import Placement

PAGE = 40

KINDS = {
    "login": "Входы",
    "review": "Отзывы",
    "comment": "Комментарии",
    "upload": "Материалы и книги",
    "moderation": "Модерация",
    "shop": "Магазин и кейсы",
    "wallet": "Кошелёк",
    "print": "Печать",
    "wall": "Стена",
}


@dataclass
class Event:
    when: datetime
    kind: str
    who: User | None
    text: str
    target: str = ""
    href: str = ""
    quote: str = ""
    image: object = None  # картинка отзыва или комментария (поле ImageField)
    meta: str = ""
    bad: bool = False
    obj: object = None
    actions: list = field(default_factory=list)


def _of(qs, people, path):
    return qs if people is None else qs.filter(**{f"{path}__in": people})


LOGIN_TEXT = {
    LoginEvent.Kind.LOGIN: "вход",
    LoginEvent.Kind.LOGOUT: "выход",
    LoginEvent.Kind.FAILED: "неверный пароль",
    LoginEvent.Kind.STOPPED: "остановлен ограничителем",
}


def logins(before, limit, people):
    qs = _of(LoginEvent.objects.select_related("user").filter(created__lt=before), people, "user")
    return [
        Event(
            e.created, "login", e.user, LOGIN_TEXT[e.kind], meta=f"{e.ip or 'адрес неизвестен'} · {e.where()}",
            bad=e.kind in (LoginEvent.Kind.FAILED, LoginEvent.Kind.STOPPED),
        )
        for e in qs.order_by("-created")[:limit]
    ]


def reviews(before, limit, people):
    qs = _of(Review.objects.select_related("author", "teacher").filter(created__lt=before), people, "author")
    events = []
    for r in qs.order_by("-created")[:limit]:
        meta = " · ".join(part for part in (
            "анонимно" if r.hide_author else "", "" if r.is_detailed() else "только оценки",
        ) if part)
        events.append(Event(
            r.created, "review", r.author, "отзыв о преподавателе", str(r.teacher), r.teacher.get_absolute_url(),
            quote=r.text, image=r.image or None, meta=meta, obj=r,
        ))
    return events


def where_comment(comment):
    """(подпись, название, ссылка) места, где оставлен комментарий."""
    if comment.material_id:
        return "комментарий к материалу", f"«{comment.material.title}»", comment.material.get_absolute_url()
    lecture = comment.lecture
    href = f"{reverse('playlist_detail', args=[lecture.playlist_id])}?lecture={lecture.pk}"
    return "комментарий к лекции", f"«{lecture.title}»", href


def comments(before, limit, people):
    qs = _of(
        Comment.objects.select_related("author", "material", "lecture").filter(created__lt=before), people, "author",
    )
    events = []
    for c in qs.order_by("-created")[:limit]:
        text, target, href = where_comment(c)
        meta = " · ".join(part for part in ("ответ" if c.parent_id else "", "анонимно" if c.hide_author else "") if part)
        events.append(Event(
            c.created, "comment", c.author, text, target, href, quote=c.text, image=c.image or None, meta=meta, obj=c,
        ))
    return events


# Модель → (как назвать вещь, «опубликован», «отклонён») — с родом самой вещи.
WORKS = {
    Material: ("материал", "опубликован", "отклонён"),
    Book: ("книга", "опубликована", "отклонена"),
    Playlist: ("курс лекций", "опубликован", "отклонён"),
}


def uploads(before, limit, people):
    events = []
    for model, (noun, _, _) in WORKS.items():
        qs = _of(model.objects.select_related("uploader").filter(created__lt=before), people, "uploader")
        for item in qs.order_by("-created")[:limit]:
            meta = item.get_status_display() + (" · анонимно" if getattr(item, "hide_uploader", False) else "")
            events.append(Event(
                item.created, "upload", item.uploader, noun, f"«{item.title}»", item.get_absolute_url(),
                meta=meta, obj=item,
            ))
    return events


def decisions(before, limit, people):
    events = []
    for model, (noun, approved, rejected) in WORKS.items():
        qs = _of(
            model.objects.select_related("uploader", "reviewed_by")
            .filter(reviewed_at__lt=before, reviewed_by__isnull=False),
            people, "reviewed_by",
        )
        for item in qs.order_by("-reviewed_at")[:limit]:
            verdict = approved if item.status == Moderated.Status.APPROVED else rejected
            meta = f"автор: {item.uploader.full_name}" if item.uploader else ""
            if item.status == Moderated.Status.REJECTED and item.review_note:
                meta += f" · «{item.review_note}»"
            events.append(Event(
                item.reviewed_at, "moderation", item.reviewed_by, f"проверка: {verdict} {noun}", f"«{item.title}»",
                item.get_absolute_url(), meta=meta, obj=item,
            ))
    return events


def shop(before, limit, people):
    opened = _of(CaseOpening.objects.select_related("user", "case", "item").filter(created__lt=before), people, "user")
    events = []
    for o in opened.order_by("-created")[:limit]:
        prize = f"«{o.item.name}», {o.get_rarity_display()}" if o.item else f"собранная ступень, вернули {o.refund}"
        events.append(Event(
            o.created, "shop", o.user, "кейс", f"«{o.case.name}»", reverse("shop"),
            quote="", meta=f"{prize} · " + (f"−{o.paid}" if o.paid else "первый — бесплатно"),
        ))
    bought = _of(
        BalanceLog.objects.select_related("wallet__user").filter(reason=BalanceLog.Reason.PURCHASE, created__lt=before),
        people, "wallet__user",
    )
    for b in bought.order_by("-created")[:limit]:
        events.append(Event(b.created, "shop", b.wallet.user, "покупка", f"«{b.note}»", reverse("shop"), meta=str(b.amount)))
    return events


def wallet(before, limit, people):
    qs = _of(
        BalanceLog.objects.select_related("wallet__user", "by").filter(reason=BalanceLog.Reason.MANUAL, created__lt=before),
        people, "wallet__user",
    )
    events = []
    for b in qs.order_by("-created")[:limit]:
        meta = f"«{b.note}»" if b.note else ""
        if b.by:
            meta += f" · провёл(а) {b.by.full_name}"
        events.append(Event(b.created, "wallet", b.wallet.user, "токены вручную", f"{b.amount:+}", meta=meta.strip(" ·")))
    return events


def prints(before, limit, people):
    qs = _of(PrintJob.objects.select_related("user").filter(submitted_at__lt=before), people, "user")
    return [
        Event(
            j.submitted_at, "print", j.user, "печать", f"«{j.name}»",
            reverse("admin:printing_printjob_change", args=[j.pk]),
            meta=f"{j.sides} {plural(j.sides, 'страница,страницы,страниц')} · {j.get_status_display()} · {rubles(j.cost)}",
            bad=j.status == PrintJob.Status.FAILED,
        )
        for j in qs.order_by("-submitted_at")[:limit]
    ]


def wall(before, limit, people):
    """Ходы на Стене — по человеку и часу: иначе один рисунок занял бы всю ленту.
    Время группы — её последний ход; по нему же и курсор, так что группа не делится между порциями."""
    rows = list(
        _of(Placement.objects.all(), people, "user")
        .annotate(hour=TruncHour("created")).values("user", "hour", "board__title")
        .annotate(n=Count("id"), last=Max("created")).filter(last__lt=before)
        .order_by("-last")[:limit]
    )
    names = User.objects.in_bulk({row["user"] for row in rows})
    return [
        Event(
            row["last"], "wall", names.get(row["user"]), "Стена",
            f"{row['n']} {plural(row['n'], 'клетка,клетки,клеток')}", reverse("wall"), meta=f"доска «{row['board__title']}»",
        )
        for row in rows
    ]


SOURCES = {
    "login": logins, "review": reviews, "comment": comments, "upload": uploads, "moderation": decisions,
    "shop": shop, "wallet": wallet, "print": prints, "wall": wall,
}


def events(before, kinds=None, people=None, limit=PAGE):
    """Порция ленты: по `limit` из каждого источника, слитые и обрезанные. people — None
    (все) или набор id."""
    merged = []
    for kind in kinds or SOURCES:
        merged.extend(SOURCES[kind](before, limit, people))
    merged.sort(key=lambda event: event.when, reverse=True)
    return merged[:limit]
