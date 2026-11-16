"""Кейсы: заплатил токены — выпала случайная вещь из набора. Деньги и вещь меняются местами
целиком или никак, как в buy(). Исход решает сервер до анимации: рулетка его только показывает.
"""
import secrets

from django.db import transaction
from django.utils import timezone

from core.context_processors import party
from economy.models import BalanceLog
from economy.services import NotEnoughFunds, lock, spend

from .models import Case, CaseOpening, CosmeticItem, UserItem
from .services import grant

R = CosmeticItem.Rarity

# Шансы ступеней в процентах, общие на все кейсы.
ODDS = {R.COMMON: 55, R.RARE: 25, R.EPIC: 12, R.LEGENDARY: 6, R.MYTHICAL: 2}

_luck = secrets.SystemRandom()


class CaseClosed(Exception):
    """Кейс выключен в админке: открыть его нельзя и по старой ссылке."""


class AllCollected(Exception):
    """Всё из кейса у человека уже есть — платить не за что."""


def odds(items):
    """Шансы ступеней в наборе: [(ступень, доля)] от обычной к мифической. Отсутствующая
    ступень своих процентов не забирает — они делятся между остальными. Что у человека уже
    есть, шансы НЕ двигает: иначе, скупив дешёвое в магазине, он поднимал бы шанс на редкое."""
    present = {item.rarity for item in items}
    weights = [(rarity, ODDS[rarity]) for rarity in CosmeticItem.RARITY_ORDER if rarity in present and rarity in ODDS]
    total = sum(weight for _, weight in weights)
    return [(rarity, weight / total) for rarity, weight in weights]


def draw(items, owned, gift=False, luck=_luck):
    """Жребий: (ступень, вещь); вещь None — ступень у человека собрана целиком. Внутри ступени —
    только то, чего ещё нет. Подарочный кейс пустым не бывает: ступени — из несобранных."""
    pool = [item for item in items if item.pk not in owned] if gift else items
    chances = odds(pool)
    rarity = luck.choices([rarity for rarity, _ in chances], weights=[share for _, share in chances])[0]
    fresh = [item for item in items if item.rarity == rarity and item.pk not in owned]
    return rarity, (luck.choice(fresh) if fresh else None)


def gift(user):
    """Почему следующий кейс бесплатный, или None. «first» — первый кейс, раз на человека.
    «party» — ещё один в праздник после взлома (quest), сверх первого."""
    openings = CaseOpening.objects.filter(user=user)
    if not openings.exists():
        return "first"
    if party():
        day = timezone.localdate()
        allowed = 1 if openings.filter(created__date__lt=day).exists() else 2
        if openings.filter(created__date=day, paid=0).count() < allowed:
            return "party"
    return None


@transaction.atomic
def open_case(user, case, luck=_luck):
    """Открыть кейс → CaseOpening. Кошелёк — первым, как в buy(): иначе два нажатия оба сочли бы
    кейс подарочным. Хватать должно на полную цену: исход решается после проверки."""
    wallet = lock(user)
    if not case.active:
        raise CaseClosed(case.name)
    items = list(case.items.all())
    owned = set(UserItem.objects.filter(user=user).values_list("item_id", flat=True))
    if all(item.pk in owned for item in items):
        raise AllCollected(case.name)
    reason = gift(user)
    price = 0 if reason else case.price
    if wallet.balance < price:
        raise NotEnoughFunds(f"нужно {price}, на балансе {wallet.balance}")

    rarity, item = draw(items, owned, gift=bool(reason), luck=luck)
    # Ступень собрана — вещи нет, половина цены остаётся. Одной строкой в журнале: плюс записался
    # бы в заработанное.
    refund = 0 if item else price // 2
    if item:
        grant(user, item)
    if price - refund:
        what = f"«{item.name}»" if item else f"{R(rarity).label}: всё уже есть, половина цены назад"
        spend(user, price - refund, BalanceLog.Reason.CASE, note=f"кейс «{case.name}» → {what}"[:200], key=f"case:{case.pk}")
    opening = CaseOpening.objects.create(user=user, case=case, item=item, rarity=rarity, paid=price, refund=refund)
    opening.gift = reason  # подпись под рулеткой: «первый» или «праздничный» (cosmetics/_roulette.html)
    return opening


def percent(share):
    """Доля словами: 0.55 → «55», 0.6875 → «68,8»."""
    return f"{share * 100:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def on_show(user, coins):
    """Кейсы для витрины с пометками: `cost` (подарок — 0), `gift`, `short` — сколько не хватает,
    `collected`, `size`, `chances` [(ступень, подпись, доля, проценты)] и `fan` — три самые редкие
    вещи для обложки. Пустые кейсы не показываем."""
    owned = set(UserItem.objects.filter(user=user).values_list("item_id", flat=True))
    reason = gift(user)
    rank = {rarity: step for step, rarity in enumerate(CosmeticItem.RARITY_ORDER)}
    shown = []
    for case in Case.objects.filter(active=True).prefetch_related("items"):
        items = list(case.items.all())
        if not items:
            continue
        case.gift = reason
        case.cost = 0 if reason else case.price
        case.short = max(case.cost - coins, 0)
        case.size = len(items)
        case.collected = all(item.pk in owned for item in items)
        case.chances = [
            (rarity, R(rarity).label, f"{share * 100:.2f}", percent(share)) for rarity, share in odds(items)
        ]
        case.fan = sorted(items, key=lambda item: (-rank.get(item.rarity, len(rank)), item.name.casefold()))[:3]
        shown.append(case)
    return shown


# Лента рулетки: плиток, на какой встаёт (хвост справа виден) и сколько разных картинок —
# лента грузится целиком, рамка весит до трёх мегабайт.
REEL = 40
STOP = 33
FACES = 10

# Шансы ступеней в ЛЕНТЕ — только для вида, исход решает draw() по ODDS. Редкие мелькают чаще.
REEL_ODDS = {R.COMMON: 50, R.RARE: 25, R.EPIC: 12, R.LEGENDARY: 9, R.MYTHICAL: 4}


def _faces(items, prize, luck):
    """Вещи ленты: {ступень: [вещи]}, выигрыш и до FACES − 1 других. Сначала по одной каждой
    ступени, остальное — по шансам ленты: наугад из набора единственная обычная мелькала бы
    через плитку."""
    pool = {rarity: [] for rarity in CosmeticItem.RARITY_ORDER}
    for item in luck.sample(items, len(items)):
        if item.pk != prize.pk:
            pool.setdefault(item.rarity, []).append(item)
    faces = {rarity: [] for rarity in pool}
    faces.setdefault(prize.rarity, []).append(prize)
    room = FACES - 1
    for rarity in sorted(pool, key=lambda rarity: -REEL_ODDS.get(rarity, 1)):
        if pool[rarity] and room:
            faces[rarity].append(pool[rarity].pop())
            room -= 1
    while room and any(pool.values()):
        rarity = max((r for r in pool if pool[r]), key=lambda r: REEL_ODDS.get(r, 1) / (len(faces[r]) + 1))
        faces[rarity].append(pool[rarity].pop())
        room -= 1
    return {rarity: group for rarity, group in faces.items() if group}


def reel(items, opening, luck=_luck):
    """Плитки ленты. На месте STOP — выигрыш, а если ступень была собрана — что-то из неё,
    что у человека уже есть. Ступень каждой плитки — по шансам ленты, а вещи одной ступени
    идут по кругу: одинаковые рядом не встают, и одна вещь не мелькает через плитку."""
    prize = opening.item or next(item for item in items if item.rarity == opening.rarity)
    faces = _faces(items, prize, luck)
    everyone = [item for group in faces.values() for item in group]
    turn = {rarity: luck.randrange(len(group)) for rarity, group in faces.items()}

    def other_than(*taken):
        """Любая вещь ленты, кроме названных — для места, где своя оказалась бы близнецом соседа."""
        left = [item for item in everyone if item.pk not in {one.pk for one in taken if one}]
        return luck.choice(left) if left else everyone[0]

    tiles = []
    for _ in range(REEL):
        rarity = luck.choices(list(faces), weights=[REEL_ODDS.get(rarity, 1) for rarity in faces])[0]
        turn[rarity] += 1
        tile = faces[rarity][turn[rarity] % len(faces[rarity])]
        if tiles and tile.pk == tiles[-1].pk:  # в ступени одна вещь, и она уже стоит слева
            tile = other_than(tiles[-1])
        tiles.append(tile)

    tiles[STOP] = prize
    for spot in (STOP - 1, STOP + 1):
        if tiles[spot].pk == prize.pk:
            tiles[spot] = other_than(prize, tiles[spot - 1], tiles[spot + 1] if spot + 1 < REEL else None)
    return tiles
