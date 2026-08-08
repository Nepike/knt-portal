"""Перекладывает уже загруженные файлы на непредсказуемые ключи.

Нужно один раз — тем файлам, что легли до перехода на uuid. Их пути вида
books/12/files/Зорич.pdf угадываются, и библиотеку можно было бы перебрать.

Новые загрузки уже приходят с uuid (attachments.storage.random_key), так что
повторно команда ничего не найдёт.
"""

import re

from django.core.management.base import BaseCommand

from attachments.storage import media_fields, random_key

RANDOM = re.compile(r"/[0-9a-f]{32}/")


class Command(BaseCommand):
    help = "Переводит старые файлы на ключи с uuid. Без --apply только показывает."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="без него только показывает")

    def handle(self, *args, **options):
        moved = 0
        for model, name in media_fields():
            for row in model.objects.exclude(**{name: ""}).exclude(**{f"{name}__isnull": True}):
                field = getattr(row, name)
                if not field or RANDOM.search(field.name):
                    continue

                old = field.name
                storage = field.storage
                if not storage.exists(old):
                    # Запись есть, блоба нет — так бывает после ручных чисток бакета.
                    # Не наше дело чинить, но и падать из-за этого команда не должна.
                    self.stderr.write(f"пропуск, файла нет в хранилище: {old}")
                    continue

                new = random_key(old.split("/")[0], old)
                self.stdout.write(f"{model.__name__}.{name}: {old} → {new}")
                if not options["apply"]:
                    continue

                with field.open("rb"):
                    # Отдаём сам файловый объект, а не прочитанные байты: сканы бывают
                    # на сотни мегабайт, и класть их целиком в память незачем.
                    new = storage.save(new, field)
                setattr(row, name, new)
                row.save(update_fields=[name])
                storage.delete(old)  # только после того, как ссылка в базе уже новая
                moved += 1

        verdict = "перенесено" if options["apply"] else "нашлось (запусти с --apply)"
        self.stdout.write(self.style.SUCCESS(f"{verdict}: {moved if options['apply'] else 'см. выше'}"))
