"""Починка типа содержимого у объектов, уже лежащих в хранилище.

Пекарня заливала куски через urllib, а он на PUT с телом подставляет свой заголовок —
`application/x-www-form-urlencoded`. Хранилище записывает его В САМ ОБЪЕКТ, и на попадании
в кеш nginx браузер получает именно этот тип, а не тот, что ставит приложение. Плееру
всё равно, он читает байтами, а вот книгу или картинку браузер по такому типу покажет
не тем, чем следует.

Заливка теперь объявляет тип честно (intake/views.py → tools/bake.py), но уже залитое
так и лежит — его и чинит эта команда. Одноразовая по замыслу, но идемпотентная:
повторный запуск ничего не трогает.
"""

from concurrent.futures import ThreadPoolExecutor

from django.core.files.storage import FileSystemStorage
from django.core.management.base import BaseCommand, CommandError

from attachments.storage import _under, content_type, file_storage

# Копировать объект «на себя» S3 умеет только целиком, и на многогигабайтном сырье
# такой запрос отваливается. Сырьё нам и не нужно: типом важен тот, что уходит браузеру.
MAX_COPY = 4 * 1024 ** 3
LOOK = 16  # запросов к хранилищу разом: их тысячи, а каждый — целое рукопожатие


class Command(BaseCommand):
    help = "Приводит Content-Type объектов хранилища к тому, что говорит расширение."

    def add_arguments(self, parser):
        parser.add_argument("--prefix", default="lectures", help="что чинить (по умолчанию lectures)")
        parser.add_argument("--apply", action="store_true", help="без него только показывает")

    def handle(self, *args, **options):
        storage = file_storage()
        if isinstance(storage, FileSystemStorage):
            raise CommandError("на диске тип берётся из имени файла, чинить нечего")

        prefix = options["prefix"].strip("/")
        client = storage.connection.meta.client
        bucket = storage.bucket_name
        location = f"{storage.location}/" if getattr(storage, "location", "") else ""

        keys = list(_under(storage, prefix))
        self.stdout.write(f"под «{prefix}» объектов: {len(keys)}")
        if not keys:
            return

        def wrong(key):
            """(ключ, нынешний тип, нужный тип) или None, если всё и так верно."""
            want = content_type(key)
            head = client.head_object(Bucket=bucket, Key=location + key)
            if head.get("ContentType") == want:
                return None
            if head.get("ContentLength", 0) > MAX_COPY:
                return (key, head.get("ContentType"), None)  # слишком велик, только сказать
            return (key, head.get("ContentType"), want)

        with ThreadPoolExecutor(max_workers=LOOK) as pool:
            found = [item for item in pool.map(wrong, keys) if item]

        if not found:
            self.stdout.write(self.style.SUCCESS("все типы на месте"))
            return

        seen = {}
        for _, was, want in found:
            seen[(was, want)] = seen.get((was, want), 0) + 1
        for (was, want), count in sorted(seen.items(), key=lambda pair: -pair[1]):
            arrow = f"→ {want}" if want else "→ пропустим, объект слишком велик для копии"
            self.stdout.write(f"  {count:>6}  {was or '(нет типа)'} {arrow}")

        fixable = [item for item in found if item[2]]
        if not options["apply"]:
            self.stdout.write(self.style.WARNING(f"\nэто примерка: чинить {len(fixable)} — нужен --apply"))
            return

        def fix(item):
            key, _, want = item
            # Копия на себя с REPLACE — единственный способ сменить тип, не перезаливая
            # байты. ETag и время правки при этом меняются, но кеш nginx держится
            # за адрес, а не за них, и подпись в адресе тоже не трогается.
            client.copy_object(
                Bucket=bucket, Key=location + key,
                CopySource={"Bucket": bucket, "Key": location + key},
                ContentType=want, MetadataDirective="REPLACE",
            )

        with ThreadPoolExecutor(max_workers=LOOK) as pool:
            done = sum(1 for _ in pool.map(fix, fixable))
        self.stdout.write(self.style.SUCCESS(f"\nисправлено объектов: {done}"))
