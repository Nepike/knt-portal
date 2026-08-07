from django import template
from django.utils.safestring import mark_safe

from core.markup import render

register = template.Library()


@register.filter
def markdown(text):
    """{{ material.text|markdown }} — разметка автора превращается в вычищенный HTML."""
    return mark_safe(render(text))  # noqa: S308 — на выходе nh3, сырого HTML там уже нет


@register.filter
def plural(n, forms):
    """Русская плюрализация: {{ n|plural:"отзыв,отзыва,отзывов" }}."""
    one, few, many = forms.split(",")
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
