"""Тексты материалов старого сайта → markdown в новой базе.

Отдельно от import_legacy, а не внутри него: конвертер хочется править и прогонять
заново, а материалы уже на месте и находятся по тому же id (см. шапку import_legacy).
Команда идемпотентна — каждый раз берёт исходник из старой базы, а не из своей же
прошлой работы.

    manage.py convert_legacy_text --db D:/knt-legacy/db.sqlite3
    manage.py convert_legacy_text --db ... --apply
    manage.py convert_legacy_text --db ... --show 112
"""

import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.legacy_markup import to_markdown
from materials.models import Material


class Command(BaseCommand):
    help = "Переносит тексты материалов со старого сайта, превращая Quill в markdown."

    def add_arguments(self, parser):
        parser.add_argument("--db", required=True, help="db.sqlite3 старого сайта")
        parser.add_argument("--apply", action="store_true", help="без него только считает")
        parser.add_argument("--show", type=int, metavar="ID", help="показать исходник и результат")

    def handle(self, *args, **options):
        path = Path(options["db"])
        if not path.is_file():
            raise CommandError(f"нет файла базы: {path}")
        old = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

        if options["show"]:
            return self.show(old, options["show"])

        filled = blank = lost = 0
        for pk, raw in old.execute("select id, text from materials_material"):
            markdown = to_markdown(raw)
            if not markdown:
                blank += 1
                continue
            if options["apply"] and not Material.objects.filter(pk=pk).update(text=markdown):
                lost += 1  # материал не переносили или уже удалён — не наша забота
            filled += 1

        self.stdout.write(f"текстов получилось: {filled}")
        self.stdout.write(f"пустых по существу: {blank}")
        if lost:
            self.stderr.write(f"материалов нет в новой базе: {lost}")
        self.stdout.write(
            self.style.SUCCESS("записано") if options["apply"]
            else self.style.WARNING("ничего не записано — запусти с --apply")
        )

    def show(self, old, pk):
        row = old.execute("select text from materials_material where id=?", (pk,)).fetchone()
        if row is None:
            raise CommandError(f"материала #{pk} нет в старой базе")
        self.stdout.write(f"─── исходник #{pk} ───\n{row[0]}\n")
        self.stdout.write(f"─── markdown ───\n{to_markdown(row[0])}")
