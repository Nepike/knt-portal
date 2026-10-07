"""Бэкап базы руками: то же, что ночью делает расписание (core/backup.py).

    manage.py backup                   снять сейчас и убрать лишние
    manage.py backup --list            что лежит в хранилище
    manage.py backup --fetch latest    отдать дамп в stdout — для восстановления

Восстановление целиком расписано в README.
"""

import sys

from django.core.management.base import BaseCommand, CommandError

from attachments.models import human_size
from attachments.storage import file_storage
from core import backup


class Command(BaseCommand):
    help = "Бэкап базы в хранилище: снять, показать список или отдать дамп."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="только показать, что лежит")
        parser.add_argument("--fetch", metavar="ДАТА", help="отдать дамп в stdout: 2026-10-07 или latest")

    def handle(self, *args, **options):
        if options["fetch"]:
            return self.fetch(options["fetch"])
        if not options["list"]:
            try:
                key, size = backup.make()
            except backup.BackupError as error:
                raise CommandError(str(error))
            gone = backup.prune()
            self.stdout.write(self.style.SUCCESS(f"снято: {key}, {human_size(size)}; старых убрано: {len(gone)}"))
        self.show()

    def show(self):
        found = backup.stored()
        for day in sorted(found, reverse=True):
            key, size = found[day]
            self.stdout.write(f"{day:%Y-%m-%d}  {human_size(size):>10}  {key}")
        self.stdout.write(f"всего: {len(found)}")

    def fetch(self, which):
        found = backup.stored()
        days = {f"{day:%Y-%m-%d}": day for day in found}
        if which == "latest" and found:
            which = max(days)
        if which not in days:
            raise CommandError(f"нет такого бэкапа: {which}. Есть: {', '.join(sorted(days)) or 'ни одного'}")
        # Байты — мимо self.stdout: тот текстовый и перекодировал бы дамп.
        with file_storage().open(found[days[which]][0], "rb") as body:
            for chunk in body.chunks():
                sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
