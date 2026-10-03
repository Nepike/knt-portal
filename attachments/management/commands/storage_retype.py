"""Починка типа содержимого у объектов, уже лежащих в хранилище.

Тип записан В САМОМ ОБЪЕКТЕ, и на попадании в кеш nginx браузер получает именно его, а не
тот, что ставит приложение. Не тем он оказывался дважды. Пекарня заливала куски через
urllib, а он на PUT с телом подставляет `application/x-www-form-urlencoded`. А файлы людей
ложились с типом, который назвал браузер загрузившего, — то есть с каким угодно.

Заливка теперь объявляет тип сама (attachments/r2.py, uploads.sign_upload, intake/views.py),
а уже залитое чинит эта команда. Идемпотентная: повторный запуск ничего не трогает.

    manage.py storage_retype                                  примерка: всё, кроме lectures/
    manage.py storage_retype --apply --journal было.jsonl     починить, записав прежние типы
    manage.py storage_retype --restore было.jsonl --apply     вернуть как было
    manage.py storage_retype --prefix lectures                только эта папка

После починки — сбросить кеш nginx: тип лежит в нём вместе с байтами (docs/media-pipeline.md).
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.core.files.storage import FileSystemStorage
from django.core.management.base import BaseCommand, CommandError

from attachments.storage import content_type, file_storage
from attachments.uploads import under

# Копировать объект «на себя» S3 умеет только целиком, и на многогигабайтном сырье
# такой запрос отваливается. Сырьё нам и не нужно: типом важен тот, что уходит браузеру.
MAX_COPY = 4 * 1024 ** 3
LOOK = 16  # запросов к хранилищу разом: их тысячи, а каждый — целое рукопожатие
# Наборы лекций без явной просьбы обходим: это миллионы кусков, тип им ставит приёмка,
# а один только их листинг длится дольше, чем починка всего остального.
SETS = "lectures"
# REPLACE стирает у объекта все метаданные разом, а не один тип — остальное несём сами.
# Без этого починка снимала бы CacheControl, с которым объект кладёт хранилище.
KEPT = ("CacheControl", "ContentDisposition", "ContentLanguage")


class Command(BaseCommand):
    help = "Приводит Content-Type объектов хранилища к тому, что говорит расширение."

    def add_arguments(self, parser):
        parser.add_argument("--prefix", default="", help="что чинить (по умолчанию всё, кроме lectures)")
        parser.add_argument("--journal", help="файл, куда записать прежние типы; с --apply обязателен")
        parser.add_argument("--restore", help="вернуть типы по журналу прошлого запуска")
        parser.add_argument("--apply", action="store_true", help="без него только показывает")

    def handle(self, *args, **options):
        storage = file_storage()
        if isinstance(storage, FileSystemStorage):
            raise CommandError("на диске тип берётся из имени файла, чинить нечего")

        journal = Path(options["journal"]) if options["journal"] else None
        if options["apply"] and not (journal or options["restore"]):
            raise CommandError("--apply без --journal не запускается: возвращать типы было бы не по чему")
        # Оборвавшийся запуск повторяют, и второй записал бы поверх только недочиненное.
        if journal and journal.exists():
            raise CommandError(f"{journal} уже есть — прежние типы в нём затёрлись бы")

        self.client = storage.connection.meta.client
        self.bucket = storage.bucket_name
        self.location = f"{storage.location}/" if getattr(storage, "location", "") else ""

        if options["restore"]:
            wanted = self.recorded(options["restore"])
        else:
            wanted = {key: (content_type(key), None) for key in self.keys(storage, options["prefix"].strip("/"))}
        self.stdout.write(f"объектов: {len(wanted)}")

        with ThreadPoolExecutor(max_workers=LOOK) as pool:
            found = [item for item in pool.map(self.look, wanted.items()) if item]
        if not found:
            self.stdout.write(self.style.SUCCESS("все типы на месте"))
            return

        seen = {}
        for _, head, want in found:
            pair = (self.declared(head), want)
            seen[pair] = seen.get(pair, 0) + 1
        for (was, want), count in sorted(seen.items(), key=lambda pair: -pair[1]):
            arrow = f"→ {self.spell(want)}" if want else "→ пропустим, объект слишком велик для копии"
            self.stdout.write(f"  {count:>6}  {self.spell(was)} {arrow}")

        fixable = [item for item in found if item[2]]
        if journal:
            # Целиком и ДО первой копии: упади починка на середине, прежние типы уже записаны.
            rows = (
                {"key": key, "type": head.get("ContentType"), "encoding": head.get("ContentEncoding")}
                for key, head, _ in fixable
            )
            journal.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8",
            )
        if not options["apply"]:
            self.stdout.write(self.style.WARNING(f"\nэто примерка: чинить {len(fixable)} — нужен --apply"))
            return

        with ThreadPoolExecutor(max_workers=LOOK) as pool:
            done = sum(1 for _ in pool.map(self.fix, fixable))
        self.stdout.write(self.style.SUCCESS(f"\nисправлено объектов: {done}"))

    def keys(self, storage, prefix):
        """Ключи под префиксом — плоским листингом по тысяче, а не папка за папкой:
        у каждого файла папка своя (uuid в пути), и обход вглубь — это запрос на файл."""
        if prefix:
            return sorted(under(prefix))
        folders, files = storage.listdir("")
        found = set(files)
        for folder in folders:
            if folder != SETS:
                found |= under(folder)
        return sorted(found)

    def recorded(self, path):
        """Что было у объектов до починки: {ключ: (тип, кодировка)} из журнала."""
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
        return {row["key"]: (row["type"], row["encoding"]) for row in rows}

    @staticmethod
    def declared(head):
        return head.get("ContentType"), head.get("ContentEncoding")

    @staticmethod
    def spell(pair):
        kind, encoding = pair
        return (kind or "(нет типа)") + (f" + {encoding}" if encoding else "")

    def look(self, item):
        """(ключ, что у объекта сейчас, что должно быть) или None, если всё и так верно.

        Кодировка — часть того же вопроса: storages дописывал её по хвосту имени, и архив
        `.tar.gz` браузер молча распаковывал при скачивании.
        """
        key, want = item
        head = self.client.head_object(Bucket=self.bucket, Key=self.location + key)
        if self.declared(head) == want:
            return None
        if head.get("ContentLength", 0) > MAX_COPY:
            return (key, head, None)  # слишком велик, только сказать
        return (key, head, want)

    def fix(self, item):
        key, head, (kind, encoding) = item
        meta = {name: head[name] for name in KEPT if head.get(name)}
        if kind:
            meta["ContentType"] = kind
        if encoding:
            meta["ContentEncoding"] = encoding
        # Копия на себя с REPLACE — единственный способ сменить тип, не перезаливая
        # байты. ETag и время правки при этом меняются, но кеш nginx держится
        # за адрес, а не за них, и подпись в адресе тоже не трогается.
        self.client.copy_object(
            Bucket=self.bucket, Key=self.location + key,
            CopySource={"Bucket": self.bucket, "Key": self.location + key},
            Metadata=head.get("Metadata", {}), MetadataDirective="REPLACE", **meta,
        )
