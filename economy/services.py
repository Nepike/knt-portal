"""Операции с валютой — только через эти функции. Правда — в журнале, баланс в кошельке —
кэш; запись — парой «строка журнала + новый кэш» под блокировкой кошелька.

`credit` — начислить, `spend` — потратить (не ниже нуля), `reclaim` — забрать начисленное
(может увести в минус).
"""

from django.db import transaction
from django.db.models import Sum

from .models import BalanceLog, Wallet


class NotEnoughFunds(Exception):
    """Списание не прошло: на балансе меньше, чем просят."""


def wallet_of(user):
    return Wallet.objects.get_or_create(user=user)[0]


def lock(user):
    """Занять кошелёк до конца транзакции. Нужен и пересчёту наград: иначе два вызова прочли
    бы один журнал и заплатили дважды."""
    wallet_of(user)  # у человека, который ещё ничего не заработал, кошелька нет
    return Wallet.objects.select_for_update().get(user=user)


def credit(user, amount, reason, note="", key="", by=None):
    """Начислить. amount — положительное число, key — за что именно (см. BalanceLog.key),
    by — кто провёл руками."""
    if amount <= 0:
        raise ValueError("начисление должно быть положительным")
    return _move(user, amount, reason, note, key, by=by)


def spend(user, amount, reason, note="", key="", by=None):
    """Списать. amount тоже положительный — знак ставим сами."""
    if amount <= 0:
        raise ValueError("списание должно быть положительным")
    return _move(user, -amount, reason, note, key, by=by)


def reclaim(user, amount, reason, note="", key=""):
    """Забрать начисленное назад; amount положительный. Единственная операция, уводящая
    в минус: иначе «получил, потратил, удалил» было бы бесплатным."""
    if amount <= 0:
        raise ValueError("возврат должен быть положительным")
    return _move(user, -amount, reason, note, key, debt=True)


@transaction.atomic
def _move(user, delta, reason, note, key="", debt=False, by=None):
    wallet = lock(user)
    balance = wallet.balance + delta
    # Отказ — только списанию: начислить кошельку в минусе можно.
    if delta < 0 and balance < 0 and not debt:
        raise NotEnoughFunds(f"нужно {-delta}, на балансе {wallet.balance}")
    wallet.balance = balance
    wallet.save(update_fields=["balance"])
    return BalanceLog.objects.create(
        wallet=wallet, amount=delta, reason=reason, note=note, key=key, balance_after=balance, by=by,
    )


def mismatched():
    """Кошельки, у которых кэш баланса разошёлся с журналом: [(кошелёк, сумма по журналу)].

    Минус по журналу — не поломка: так выглядит возврат награды за удалённое, если её
    успели потратить (rewards.remove)."""
    wallets = Wallet.objects.select_related("user").annotate(journal=Sum("entries__amount"))
    return [(wallet, wallet.journal or 0) for wallet in wallets if wallet.balance != (wallet.journal or 0)]


@transaction.atomic
def recount(wallet):
    """Привести кэш к журналу → (было, стало). Под той же блокировкой: иначе покупка между
    подсчётом и записью пропала бы из кэша."""
    wallet = lock(wallet.user)
    total = wallet.entries.aggregate(total=Sum("amount"))["total"] or 0
    was = wallet.balance
    if was != total:
        wallet.balance = total
        wallet.save(update_fields=["balance"])
    return was, total
