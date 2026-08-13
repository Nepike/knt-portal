"""Сверка кошельков с журналом.

Баланс в кошельке — кэш, правда лежит в BalanceLog. Расхождение означает, что баланс
меняли мимо economy.services: команда показывает такие кошельки, а по --apply
приводит кэш к журналу.

    manage.py recount_balances
    manage.py recount_balances --apply
"""

from django.core.management.base import BaseCommand
from django.db.models import Sum

from economy.models import Wallet
from economy.services import recount


class Command(BaseCommand):
    help = "Сверяет баланс кошельков с журналом операций."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="без него только показывает")

    def handle(self, *args, **options):
        wallets = list(Wallet.objects.annotate(journal=Sum("entries__amount")))
        broken = [(w, w.journal or 0) for w in wallets if w.balance != (w.journal or 0)]

        if not broken:
            self.stdout.write(self.style.SUCCESS(f"расхождений нет, кошельков: {len(wallets)}"))
            return

        for wallet, journal in broken:
            self.stdout.write(f"{wallet.user}: в кошельке {wallet.balance}, по журналу {journal}")

        # Пересчётом это не лечится: отрицательная сумма значит, что списаний записано
        # больше, чем начислений, — сначала надо понять, откуда взялись лишние строки.
        negative = [wallet for wallet, journal in broken if journal < 0]
        if negative:
            self.stdout.write(self.style.ERROR(f"журнал уходит в минус у {len(negative)} — разбираться руками"))

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("ничего не сделано — запусти с --apply"))
            return

        fixed = 0
        for wallet, journal in broken:
            if journal >= 0:
                recount(wallet)
                fixed += 1
        self.stdout.write(self.style.SUCCESS(f"поправлено: {fixed}"))
