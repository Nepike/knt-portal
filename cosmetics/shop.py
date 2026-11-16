"""Магазин: обмен токенов на вещи. Списание и выдача неделимы. Купленное НЕ надевается само:
покупают и про запас.
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


# Полки витрины по порядку; вид, которого здесь нет, встаёт в конец, а не пропадает.
SHELVES = (CosmeticItem.Kind.AVATAR_FRAME, CosmeticItem.Kind.PROFILE_BACKGROUND, CosmeticItem.Kind.PROFILE_HEADER)

_RANK = {rarity: step for step, rarity in enumerate(CosmeticItem.RARITY_ORDER)}

# Порядок внутри полки, по умолчанию — от редкой. Неизвестная ступень — самая редкая, как в cost.
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
    """Одна полка: отбор и порядок. «По карману» — денег хватает и вещи ещё нет."""
    if affordable:
        items = [item for item in items if not item.owned and not item.short]
    return sorted(items, key=ORDERS[order])


def showcase(items, order=DEFAULT_ORDER, affordable=False):
    """Витрина полками: [(вид, подпись, вещи)]. Полка остаётся и пустой после отбора."""
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
    """Купить вещь → запись инвентаря. Кошелёк — первым: иначе два нажатия прочли бы один баланс.
    Второе упрётся в «уже есть» или в `one_copy_per_person` — и транзакция откатится."""
    lock(user)
    if not item.sold:
        raise NotForSale(f"«{item.name}» не продаётся")
    if UserItem.objects.filter(user=user, item=item).exists():
        raise AlreadyOwned(f"«{item.name}» уже есть")

    spend(user, item.cost, BalanceLog.Reason.PURCHASE, note=item.name, key=f"item:{item.pk}")
    return grant(user, item)
