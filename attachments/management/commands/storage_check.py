from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from attachments.storage import media_storage

PAYLOAD = b"knt storage check"


class Command(BaseCommand):
    help = "Проверяет хранилище файлов: запись, чтение, ссылку, удаление."

    def handle(self, *args, **options):
        storage = media_storage()
        self.stdout.write(f"хранилище: {type(storage).__name__}")
        if settings.R2_BUCKET:
            self.stdout.write(f"бакет: {settings.R2_BUCKET} на {settings.R2_OPTIONS['endpoint_url']}")

        name = storage.save("checks/storage-check.txt", ContentFile(PAYLOAD))
        try:
            self.stdout.write(f"записан: {name}")
            with storage.open(name) as saved:
                body = saved.read()
            if body != PAYLOAD:
                raise CommandError(f"прочиталось не то, что писали: {body!r}")
            self.stdout.write(f"прочитан: {len(body)} байт")
            self.stdout.write(f"ссылка: {storage.url(name)}")
        finally:
            storage.delete(name)

        self.stdout.write(self.style.SUCCESS("хранилище работает"))
