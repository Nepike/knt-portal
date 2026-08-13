"""Цвет со Стены в остальном сайте.

Пока живёт только на странице профиля: по сайту метку разносить не стали до
этапа косметики, а смысл она наберёт, если цвет снова закрепят за человеком
(rules.OWN_COLOR_ONLY).
"""

from django import template

from .. import palette

register = template.Library()


@register.inclusion_tag("wall/_dot.html")
def wall_dot(person, named=False):
    """Кружок цвета рядом с именем; named — ещё и название рядом.

    Профиль на Стене заводится при первом заходе туда, так что у многих его нет —
    тогда и метки не будет. Запрос лишний: где метка идёт списком, добавляйте
    select_related("wall") к выборке.
    """
    profile = getattr(person, "wall", None)
    return {"shade": palette.get(profile.color) if profile else None, "named": named}
