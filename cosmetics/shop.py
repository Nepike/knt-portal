"""Магазин: обмен токенов на вещи.

Отдельно от services.py: там инвентарь и слоты, здесь деньги. Списание и выдача обязаны
быть неделимы — иначе на полпути возможен вариант «токены ушли, вещи нет».

Купленное падает в инвентарь и НЕ надевается само (решение пользователя): человек может
покупать про запас, а подмена надетого без спроса выглядела бы поломкой.
"""

from django.db import transaction

from economy.models import BalanceLog
from economy.services import lock, spend

from .models import CosmeticItem, UserItem, slot_heading
from .services import grant


class AlreadyOwned(Exception):
    """Второй экземпляр той же вещи не нужен: она косметическая, носить можно одну."""


class NotForSale(Exception):
    """Вещь есть, но в витрине её нет — раздаётся кейсами или руками."""


def on_sale():
    """Всё, что продаётся. Порядок на полках задаёт arrange — его выбирает человек."""
    return CosmeticItem.objects.filter(sold=True)


# Полки витрины по порядку. Вид, которого здесь нет, встаёт в конец: новый вид вещи
# не должен пропасть из магазина только потому, что сюда его забыли дописать.
SHELVES = (CosmeticItem.Kind.AVATAR_FRAME, CosmeticItem.Kind.PROFILE_BACKGROUND, CosmeticItem.Kind.PROFILE_HEADER)

_RANK = {rarity: step for step, rarity in enumerate(CosmeticItem.RARITY_ORDER)}

# Порядок внутри полки. По умолчанию — от редкой: витрина открывается тем, на что хочется
# смотреть, а дешёвое находит галочка «мне по карману». Неизвестная ступень — самая редкая,
# как и в цене (CosmeticItem.cost).
ORDERS = {
    "rare": lambda item: (-_RANK.get(item.rarity, len(_RANK)), item.name.casefold()),
    "cheap": lambda item: (item.cost, item.name.casefold()),
}
DEFAULT_ORDER = "rare"


def marked(user, coins):
    """Вещи в продаже с пометками для витрины: `owned` — уже есть, `short` — сколько не хватает."""
    owned = set(UserItem.objects.filter(user=user).values_list("item_id", flat=True))
    items = list(on_sale())
    for item in items:
        item.owned = item.pk in owned
        item.short = max(item.cost - coins, 0)
    return items


def arrange(items, order=DEFAULT_ORDER, affordable=False):
    """Одна полка: отбор и порядок. «По карману» — то, что можно купить прямо сейчас:
    денег хватает и вещи ещё нет."""
    if affordable:
        items = [item for item in items if not item.owned and not item.short]
    return sorted(items, key=ORDERS[order])


def showcase(items, order=DEFAULT_ORDER, affordable=False):
    """Витрина полками: [(вид, подпись, вещи)]. Полка остаётся и пустой после отбора —
    человек должен видеть, что фоны в магазине есть, просто пока не по карману."""
    kinds = sorted(
        {item.kind for item in items},
        key=lambda kind: (SHELVES.index(kind) if kind in SHELVES else len(SHELVES), kind),
    )
    return [
        (kind, slot_heading(kind), arrange([item for item in items if item.kind == kind], order, affordable))
        for kind in kinds
    ]


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
