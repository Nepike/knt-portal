"""Пересчёт токенов по всем: доначислить за сделанное (тот же `sync`). Повторный запуск
ничего не меняет.

    manage.py recount_tokens           # пробный прогон, ничего не пишет
    manage.py recount_tokens --apply   # доначислить недостающее

Сноса журнала нет: после открытия магазина он вернул бы токены за покупки. Пересчёт вниз —
отдельной командой под задачу.
"""

from django.core.management.base import BaseCommand

from economy import rewards
from economy.models import BalanceLog
from users.models import User

TOP = 10


class Command(BaseCommand):
    help = "Начислить токены за вклад: материалы, книги, отзывы, скачивания, Стену, модерацию"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="записать (без него — пробный прогон)")

    def handle(self, *args, **options):
        apply = options["apply"]

        # Всем живым: стартовые полагаются каждому.
        people = User.objects.filter(is_active=True).order_by("pk")
        totals, rich = self.walk(people, apply)

        self.report(totals, rich, people.count())
        if not apply:
            self.stdout.write(self.style.WARNING("\nПробный прогон. Записать: --apply"))

    def walk(self, people, apply):
        """Кому сколько причитается. Без --apply ничего не пишем, поэтому «что уже
        выплачено» на пробном прогоне читаем сами, а не полагаемся на sync."""
        totals, rich = {}, []
        for person in people.iterator(chunk_size=200):
            if apply:
                added = rewards.sync(person)
            else:
                added = {}
                for award, gap in rewards.pending(person):
                    added[award.reason] = added.get(award.reason, 0) + gap
            if added:
                rich.append((sum(added.values()), person.full_name))
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

        # Медиана: пара тяжеловесов утягивает среднее.
        amounts = sorted(amount for amount, _ in rich)
        self.stdout.write(f"\nМедиана: {amounts[len(amounts) // 2]}, минимум: {amounts[0]}, максимум: {amounts[-1]}")
