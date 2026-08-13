"""Операции с валютой. Всё, что меняет баланс, идёт только через эти функции.

Правда о деньгах лежит в журнале, поле в кошельке — лишь кэш. Поэтому запись всегда
идёт парой «строка журнала + новый кэш», и обе под блокировкой строки кошелька:
два одновременных списания иначе прочитали бы один и тот же баланс и потратили дважды.
"""

from django.db import transaction
from django.db.models import Sum

from .models import BalanceLog, Wallet


class NotEnoughFunds(Exception):
    """Списание не прошло: на балансе меньше, чем просят."""


def wallet_of(user):
    return Wallet.objects.get_or_create(user=user)[0]


def credit(user, amount, reason, note=""):
    """Начислить. amount — положительное число."""
    if amount <= 0:
        raise ValueError("начисление должно быть положительным")
    return _move(user, amount, reason, note)


def spend(user, amount, reason, note=""):
    """Списать. amount тоже положительный — знак ставим сами."""
    if amount <= 0:
        raise ValueError("списание должно быть положительным")
    return _move(user, -amount, reason, note)


@transaction.atomic
def _move(user, delta, reason, note):
    wallet_of(user)  # у человека, который ещё ничего не заработал, кошелька нет
    wallet = Wallet.objects.select_for_update().get(user=user)
    balance = wallet.balance + delta
    if balance < 0:
        raise NotEnoughFunds(f"нужно {-delta}, на балансе {wallet.balance}")
    wallet.balance = balance
    wallet.save(update_fields=["balance"])
    return BalanceLog.objects.create(
        wallet=wallet, amount=delta, reason=reason, note=note, balance_after=balance,
    )


def recount(wallet):
    """Привести кэш к журналу. Возвращает, что было и что стало."""
    total = wallet.entries.aggregate(total=Sum("amount"))["total"] or 0
    was = wallet.balance
    if was != total:
        wallet.balance = total
        wallet.save(update_fields=["balance"])
    return was, total
