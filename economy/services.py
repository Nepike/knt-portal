"""Операции с валютой. Всё, что меняет баланс, идёт только через эти функции.

Правда о деньгах лежит в журнале, поле в кошельке — лишь кэш. Поэтому запись всегда
идёт парой «строка журнала + новый кэш», и обе под блокировкой строки кошелька:
два одновременных списания иначе прочитали бы один и тот же баланс и потратили дважды.

Операций три: начислить (`credit`), потратить (`spend` — не ниже нуля) и забрать
начисленное назад (`reclaim` — может увести в минус).
"""

from django.db import transaction
from django.db.models import Sum

from .models import BalanceLog, Wallet


class NotEnoughFunds(Exception):
    """Списание не прошло: на балансе меньше, чем просят."""


def wallet_of(user):
    return Wallet.objects.get_or_create(user=user)[0]


def lock(user):
    """Занять кошелёк до конца транзакции.

    Нужен не только списанию: пересчёт наград сперва читает журнал, а потом дописывает
    разницу, и без блокировки два одновременных вызова прочитали бы один и тот же журнал
    и заплатили дважды.
    """
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
    """Забрать начисленное назад. amount положительный — знак ставим сами.

    Единственная операция, которая уводит баланс в минус. Трата обязана упираться в
    ноль, а возврат — нет: иначе «получил награду, потратил, удалил» оставалось бы
    бесплатным, и удалять выгодно было бы именно с пустым кошельком.
    """
    if amount <= 0:
        raise ValueError("возврат должен быть положительным")
    return _move(user, -amount, reason, note, key, debt=True)


@transaction.atomic
def _move(user, delta, reason, note, key="", debt=False, by=None):
    wallet = lock(user)
    balance = wallet.balance + delta
    # Только списание: начисление кошельку в минусе его ещё не закрывает, но отказывать
    # в нём незачем.
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


def recount(wallet):
    """Привести кэш к журналу. Возвращает, что было и что стало."""
    total = wallet.entries.aggregate(total=Sum("amount"))["total"] or 0
    was = wallet.balance
    if was != total:
        wallet.balance = total
        wallet.save(update_fields=["balance"])
    return was, total
