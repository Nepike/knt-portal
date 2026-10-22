from django import template

from printing.pricing import rubles

register = template.Library()


@register.filter
def rub(kopecks):
    """Копейки → «1,24 ₽»."""
    return rubles(int(kopecks or 0))
