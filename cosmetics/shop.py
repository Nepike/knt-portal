"""Магазин: обмен токенов на вещи. Списание и выдача неделимы. Купленное НЕ надевается само:
покупают и про запас.
"""

from copy import copy

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


# Глитч «сайт взломан» (quest): все дни взлома на месте каждой вещи — призовая рамка за эту цену.
HACKED_PRICE = 99_999_999


def hacked():
    """Призовая рамка, если витрина сейчас «взломана», иначе None. Рамку не назначили — витрина
    обычная."""
    from core.context_processors import today
    from quest.models import Broadcast

    if today()[0] != "glitch":
        return None
    broadcast = Broadcast.current()
    return broadcast.frame if broadcast else None


def masked(items, prize, coins, owned):
    """Подменить вещи призовой рамкой за HACKED_PRICE. pk остаётся от вещи: карточка и листание
    идут по настоящей полке. owned — рамка уже у человека (вскрыл ключ)."""
    stands = []
    for item in items:
        stand = copy(prize)
        stand.pk, stand.price = item.pk, HACKED_PRICE
        stand.owned, stand.short = owned, max(HACKED_PRICE - coins, 0)
        stands.append(stand)
    return stands


def showcase(items, order=DEFAULT_ORDER, affordable=False, prize=None, coins=0, owned=False):
    """Витрина полками: [(вид, подпись, вещи)]. Полка остаётся и пустой после отбора. prize —
    взломанная витрина (`hacked`): полки прежние, на них подмена (`masked`)."""
    kinds = sorted(
        {item.kind for item in items},
        key=lambda kind: (SHELVES.index(kind) if kind in SHELVES else len(SHELVES), kind),
    )
    shelves = []
    for kind in kinds:
        shelf = [item for item in items if item.kind == kind]
        if prize:
            shelf = masked(shelf, prize, coins, owned)
        shelves.append((kind, slot_heading(kind), arrange(shelf, order, affordable)))
    return shelves


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
