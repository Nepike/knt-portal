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
    """Текст сообщения со ссылками, по которым можно нажать.

    Ссылками в чате перекидываются постоянно, а до этого их приходилось выделять и
    копировать руками. `urlize` СНАЧАЛА экранирует текст (autoescape=True), поэтому
    разметка из сообщения сюда не просачивается — наружу уходят только теги, которые
    он поставил сам.

    Открываем в новой вкладке: уход по ссылке иначе теряет место в переписке. Своего
    параметра у urlize для этого нет, поэтому дописываем к тому, что он сгенерировал.
    """
    return mark_safe(  # noqa: S308 — на входе экранированный текст, теги только от urlize
        urlize(text, nofollow=True, autoescape=True).replace("<a href=", '<a target="_blank" href=')
    )


@register.filter
def body(message):
    """Текст сообщения целиком: ссылки кликабельны, упоминания ведут в профиль.

    Упоминания подставляем ПОСЛЕ urlize и по готовому списку (Message.mentions), а не
    ищем в тексте собачку глазами шаблона: кого позвали, решено один раз при сохранении
    (chats/mentions.py), и подделать подсветку набором чужого имени нельзя.

    Ищем уже экранированную форму — html к этому месту прошёл через urlize, и имя с
    кавычкой или амперсандом в нём выглядит иначе, чем в базе.
    """
    html = links(message.text)
    for person in message.mentions.all():
        tag = escape(f"@{person.full_name}")
        link = f'<a href="{reverse("profile", args=[person.pk])}" class="font-medium !no-underline">{tag}</a>'
        html = html.replace(tag, link)
    return mark_safe(html)  # noqa: S308 — links() уже экранировал текст, имя экранируем сами


@register.filter
def spotlight(text, query):
    """Кусок текста вокруг найденного, с подсветкой самого совпадения.

    Кусок, а не весь текст: реплика бывает на десять строк, и совпадение в её конце
    в списке находок просто не видно. Регистр не важен — ищем так же, как база.
    """
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
    """Чем сообщение представляется там, где оно не показано целиком: в списке чатов
    и в цитате ответа.

    Обычно это его текст, но у сообщения из одних вложений текста нет вовсе — и строка
    в списке оставалась пустой, а ответ на фотографию цитировал пустоту.
    """
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
    """Отметка в списке чатов: сегодня — часы, вчера — «вчера», раньше — дата.

    Раньше здесь всегда стояли часы, и сообщение недельной давности выглядело
    сегодняшним. Место узкое (справа от превью), поэтому коротко и без года.
    """
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
