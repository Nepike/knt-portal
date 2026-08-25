"""Пересчёт токенов по всем: доначислить за то, что человек уже сделал.

Начисление — чистая функция от состояния (economy/rewards.py), поэтому команда и есть
тот же `sync`, прогнанный по всем сразу. Повторный запуск ничего не меняет: разницы нет
— в журнал не ложится ни строки.

    manage.py recount_tokens           # пробный прогон, ничего не пишет
    manage.py recount_tokens --apply   # доначислить недостающее

Сноса журнала («начислить с нуля») тут больше нет: разовую раздачу за прошлые заслуги
на проде уже провели, а после открытия магазина такой снос вернул бы людям токены за
покупки, оставив им и купленные вещи. Понадобится пересчёт вниз — писать отдельную
команду под конкретную задачу, а не держать наготове рубильник на весь журнал.
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

        # Начисляем всем живым, а не только тем, кто что-то сделал: стартовые полагаются
        # каждому, и человек без единой загрузки должен уметь купить хотя бы одну вещь.
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
