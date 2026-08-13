"""Кошелёк в шаблонах."""

from django import template

register = template.Library()


@register.simple_tag
def coins(person):
    """Сколько у человека монет.

    Кошелёк заводится первым начислением, поэтому у новичка его ещё нет — это ноль,
    а не ошибка. Обратная связь один-к-одному кидает исключение, наследованное
    от AttributeError, так что getattr с запасным значением тут работает как надо.
    """
    wallet = getattr(person, "wallet", None)
    return wallet.balance if wallet else 0
