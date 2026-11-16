"""Кошелёк в шаблонах."""

from django import template

register = template.Library()


@register.simple_tag
def coins(person):
    """Монеты человека; нет кошелька — ноль. Исключение обратной связи one-to-one наследует
    AttributeError, поэтому getattr с запасным значением работает."""
    wallet = getattr(person, "wallet", None)
    return wallet.balance if wallet else 0
