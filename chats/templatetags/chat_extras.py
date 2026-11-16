from datetime import timedelta

from django import template
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import escape, urlize
from django.utils.safestring import mark_safe

from ..models import REACTIONS

register = template.Library()


@register.filter
def links(text):
    """Текст сообщения с кликабельными ссылками. urlize СНАЧАЛА экранирует текст
    (autoescape=True) — разметка из сообщения не просачивается. target дописываем сами:
    у urlize такого параметра нет."""
    return mark_safe(  # noqa: S308 — на входе экранированный текст, теги только от urlize
        urlize(text, nofollow=True, autoescape=True).replace("<a href=", '<a target="_blank" href=')
    )


@register.filter
def body(message):
    """Текст сообщения: ссылки и упоминания. Упоминания — после urlize и по Message.mentions
    (решено при сохранении, подделать нельзя); ищем экранированную форму имени."""
    html = links(message.text)
    for person in message.mentions.all():
        tag = escape(f"@{person.full_name}")
        link = f'<a href="{reverse("profile", args=[person.pk])}" class="font-medium !no-underline">{tag}</a>'
        html = html.replace(tag, link)
    return mark_safe(html)  # noqa: S308 — links() уже экранировал текст, имя экранируем сами


@register.filter
def spotlight(text, query):
    """Кусок текста вокруг найденного с подсветкой: совпадение в конце длинной реплики иначе не видно."""
    at = text.lower().find(query.lower())
    if at == -1:
        return text
    start = max(0, at - 30)
    piece = text[start : at + len(query) + 120]
    hit = piece[at - start : at - start + len(query)]
    html = escape(piece).replace(
        escape(hit), f'<mark class="bg-accent/25 text-inherit rounded px-0.5">{escape(hit)}</mark>', 1
    )
    return mark_safe(f"{'…' if start else ''}{html}")  # noqa: S308 — и кусок, и совпадение экранированы


@register.filter
def preview(message):
    """Сообщение там, где оно не целиком (список чатов, цитата): текст, а без текста —
    по вложениям."""
    if message.deleted:
        return "сообщение удалено"
    if message.text:
        return message.text
    shots, papers = message.images.all(), message.files.all()
    if shots and papers:
        return "Вложения"
    if shots:
        return "Фото"
    # Один файл называем по имени — так понятнее, о каком именно идёт речь.
    return papers[0].name if len(papers) == 1 else "Файлы" if papers else ""


def _named(day, today):
    """«Сегодня»/«Вчера» или None — дальше зовущий подставляет дату."""
    if day == today:
        return "Сегодня"
    if day == today - timedelta(days=1):
        return "Вчера"
    return None


@register.filter
def day_label(value):
    """Дата разделителя в ленте: «Сегодня», «Вчера», «3 сентября», «3 сентября 2025»."""
    day, today = timezone.localdate(value), timezone.localdate()
    # Год пишем только у прошлых лет: в переписке этого года он лишний шум.
    return _named(day, today) or date_format(day, "j E" if day.year == today.year else "j E Y")


@register.filter
def when(value):
    """Отметка в списке чатов: сегодня — часы, вчера — «вчера», раньше — дата без года."""
    day, today = timezone.localdate(value), timezone.localdate()
    if day == today:
        return date_format(timezone.localtime(value), "H:i")
    return (_named(day, today) or date_format(day, "d.m" if day.year == today.year else "d.m.y")).lower()


@register.filter
def is_own(message, user):
    """{% with own=m|is_own:user %} — в {% with %} выражения запрещены."""
    return message.author_id == user.pk


@register.filter
def reaction_summary(message, user):
    """[(эмодзи, сколько, моя ли)] — группируем prefetched-реакции в Python."""
    grouped = {}
    for r in message.reactions.all():
        entry = grouped.setdefault(r.emoji, [0, False])
        entry[0] += 1
        if r.user_id == user.pk:
            entry[1] = True
    return [(emoji, count, mine) for emoji, (count, mine) in grouped.items()]


@register.simple_tag
def reaction_palette():
    return REACTIONS
