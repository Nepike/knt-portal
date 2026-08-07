from django.core.management.base import BaseCommand, CommandError

from core.tasks import ping

WAIT = 10


class Command(BaseCommand):
    help = "Проверяет очередь: кладёт задачу и ждёт, что воркер её выполнит."

    def handle(self, *args, **options):
        try:
            result = ping.delay("живой")
        except Exception as error:  # брокер недоступен — до воркера дело даже не дошло
            raise CommandError(f"Не достучались до брокера: {error}")

        self.stdout.write(f"задача поставлена: {result.id}")
        try:
            answer = result.get(timeout=WAIT)
        except Exception as error:
            raise CommandError(f"Воркер не ответил за {WAIT} с ({error}). Он запущен?")

        self.stdout.write(self.style.SUCCESS(f"очередь работает, воркер ответил: {answer}"))
