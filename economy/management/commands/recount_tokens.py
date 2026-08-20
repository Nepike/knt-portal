"""Пересчёт токенов по всем: начислить за то, что человек уже сделал.

Начисление — чистая функция от состояния (economy/rewards.py), поэтому команда и есть
тот же `sync`, прогнанный по всем сразу. Повторный запуск ничего не меняет: разницы нет
— в журнал не ложится ни строки.

    manage.py recount_tokens                  # пробный прогон, ничего не пишет
    manage.py recount_tokens --apply          # доначислить недостающее
    manage.py recount_tokens --reset --apply  # снести журнал и начислить с нуля

`--reset` сносит ВЕСЬ журнал операций и обнуляет балансы, а не только награды: вместе
с ними уйдут выдачи вручную и (когда появится магазин) покупки. Для первого запуска на
проде это то, что надо, дальше — только с пониманием, что стирается.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from economy import rewards
from economy.models import BalanceLog, Wallet
from users.models import User

TOP = 10


class Command(BaseCommand):
    help = "Начислить токены за вклад: материалы, книги, отзывы, скачивания, Стену, модерацию"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="записать (без него — пробный прогон)")
        parser.add_argument("--reset", action="store_true", help="сначала снести журнал и обнулить балансы")

    def handle(self, *args, **options):
        apply, reset = options["apply"], options["reset"]

        if reset:
            self.wipe(apply)

        # Начисляем всем живым, а не только тем, кто что-то сделал: стартовые полагаются
        # каждому, и человек без единой загрузки должен уметь купить хотя бы одну вещь.
        people = User.objects.filter(is_active=True).order_by("pk")
        totals, rich = self.walk(people, apply, reset)

        self.report(totals, rich, people.count())
        if not apply:
            self.stdout.write(self.style.WARNING("\nПробный прогон. Записать: --apply"))

    def wipe(self, apply):
        entries, wallets = BalanceLog.objects.count(), Wallet.objects.exclude(balance=0).count()
        self.stdout.write(f"Снести журнал: {entries} операций, обнулить кошельков: {wallets}")
        if apply:
            with transaction.atomic():
                BalanceLog.objects.all().delete()
                Wallet.objects.update(balance=0)

    def walk(self, people, apply, reset):
        """Кому сколько причитается. Без --apply ничего не пишем, поэтому «что уже
        выплачено» на пробном прогоне читаем сами, а не полагаемся на sync."""
        totals, rich = {}, []
        for person in people.iterator(chunk_size=200):
            if apply:
                added = rewards.sync(person)
            else:
                # На пробе с --reset считаем так, будто журнал уже снесён: показать
                # надо то, что получится ПОСЛЕ сноса, а сносить пока нечего.
                added = {}
                for award, gap in rewards.pending(person, fresh=reset):
                    added[award.reason] = added.get(award.reason, 0) + gap
            if added:
                rich.append((sum(added.values()), f"{person.name} {person.surname}"))
            for reason, amount in added.items():
                totals[reason] = totals.get(reason, 0) + amount
        rich.sort(reverse=True)
        return totals, rich

    def report(self, totals, rich, people):
        labels = dict(BalanceLog.Reason.choices)
        self.stdout.write(f"\nЛюдей: {people}, начисление получат: {len(rich)}\n")
        for reason, amount in sorted(totals.items(), key=lambda pair: -pair[1]):
            self.stdout.write(f"  {labels.get(reason, reason):38} {amount:>9}")
        self.stdout.write(f"  {'ВСЕГО':38} {sum(totals.values()):>9}")

        if not rich:
            return
        self.stdout.write("\nСамые крупные начисления:")
        for amount, name in rich[:TOP]:
            self.stdout.write(f"  {amount:>8}  {name}")

        # Медиана честнее среднего: пара тяжеловесов утягивает среднее так, что по нему
        # не понять, на что хватит обычному человеку.
        amounts = sorted(amount for amount, _ in rich)
        self.stdout.write(f"\nМедиана: {amounts[len(amounts) // 2]}, минимум: {amounts[0]}, максимум: {amounts[-1]}")
