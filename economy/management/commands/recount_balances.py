"""Сверка кошельков с журналом.

Баланс в кошельке — кэш, правда лежит в BalanceLog. Расхождение означает, что баланс
меняли мимо economy.services: команда показывает такие кошельки, а по --apply
приводит кэш к журналу.

    manage.py recount_balances
    manage.py recount_balances --apply
"""

from django.core.management.base import BaseCommand

from economy.models import Wallet
from economy.services import mismatched, recount


class Command(BaseCommand):
    help = "Сверяет баланс кошельков с журналом операций."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="без него только показывает")

    def handle(self, *args, **options):
        broken = mismatched()

        if not broken:
            self.stdout.write(self.style.SUCCESS(f"расхождений нет, кошельков: {Wallet.objects.count()}"))
            return

        for wallet, journal in broken:
            self.stdout.write(f"{wallet.user}: в кошельке {wallet.balance}, по журналу {journal}")

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("ничего не сделано — запусти с --apply"))
            return

        for wallet, _ in broken:
            recount(wallet)
        self.stdout.write(self.style.SUCCESS(f"поправлено: {len(broken)}"))
