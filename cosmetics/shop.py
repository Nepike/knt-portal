"""Магазин: обмен токенов на вещи.

Отдельно от services.py: там инвентарь и слоты, здесь деньги. Списание и выдача обязаны
быть неделимы — иначе на полпути возможен вариант «токены ушли, вещи нет».

Купленное падает в инвентарь и НЕ надевается само (решение пользователя): человек может
покупать про запас, а подмена надетого без спроса выглядела бы поломкой.
"""

from django.db import transaction

from economy.models import BalanceLog
from economy.services import lock, spend

from .models import CosmeticItem, UserItem
from .services import grant, rarity_rank


class AlreadyOwned(Exception):
    """Второй экземпляр той же вещи не нужен: она косметическая, носить можно одну."""


class NotForSale(Exception):
    """Вещь есть, но в витрине её нет — раздаётся кейсами или руками."""


def on_sale():
    """Витрина: тем же порядком, что и инвентарь, — блоками по видам, внутри от дешёвой
    к дорогой. Одинаковый порядок в двух списках позволяет искать вещь глазами одинаково."""
    return (
        CosmeticItem.objects.filter(sold=True)
        .annotate(rank=rarity_rank()).order_by("kind", "rank", "name")
    )


@transaction.atomic
def buy(user, item):
    """Купить вещь. Возвращает запись инвентаря.

    Кошелёк занимаем первым: иначе два нажатия подряд успели бы прочитать один и тот же
    баланс. Второе после этого упирается в проверку «уже есть», а если бы и проскочило —
    в ограничение `one_copy_per_person`, и вся транзакция откатится вместе со списанием.
    """
    lock(user)
    if not item.sold:
        raise NotForSale(f"«{item.name}» не продаётся")
    if UserItem.objects.filter(user=user, item=item).exists():
        raise AlreadyOwned(f"«{item.name}» уже есть")

    spend(user, item.cost, BalanceLog.Reason.PURCHASE, note=item.name, key=f"item:{item.pk}")
    return grant(user, item)
