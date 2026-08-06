from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from attachments.models import File
from attachments.storage import file_storage

PREFIX = "uploads"


class Command(BaseCommand):
    help = "Убирает из хранилища прямые загрузки, к которым так и не привязалась запись File."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=1, help="не трогать загруженное за последние N дней")
        parser.add_argument("--apply", action="store_true", help="без него только показывает, что удалил бы")

    def handle(self, *args, **options):
        storage = file_storage()
        cutoff = timezone.now() - timedelta(days=options["days"])
        known = set(File.objects.filter(file__startswith=f"{PREFIX}/").values_list("file", flat=True))

        folders, _ = storage.listdir(PREFIX)
        found = size = 0
        for folder in folders:
            for name in storage.listdir(f"{PREFIX}/{folder}")[1]:
                key = f"{PREFIX}/{folder}/{name}"
                if key in known or storage.get_modified_time(key) > cutoff:
                    continue
                found += 1
                size += storage.size(key)
                self.stdout.write(key)
                if options["apply"]:
                    storage.delete(key)

        # Окно печатаем всегда: без него «удалено: 0» выглядит поломкой, хотя сироты
        # просто моложе --days (свежую загрузку ещё может подобрать открытая форма).
        window = f"старше {options['days']} дн" if options["days"] else "любого возраста"
        verdict = "удалено" if options["apply"] else "нашлось (запусти с --apply)"
        self.stdout.write(self.style.SUCCESS(f"{verdict}: {found} ({window}), {size // 1024 // 1024} МБ"))
