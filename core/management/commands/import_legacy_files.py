"""Раскладывает по хранилищу файлы, выгруженные со старого local.inbicst.ru.

Основной перенос (import_legacy) оставил часть записей File без блоба: сервер с файлами
тогда не отвечал, и в базу лёг только сам факт файла. Теперь его выгрузили целиком —
одной плоской папкой, — и эта команда разносит её по хранилищу под новыми ключами.

Что с чем связывать, знает старая база: имя файла в выгрузке — это последний сегмент
`system_file.remote_link`, а id там тот же, что у File. Собрать это имя самим нельзя:
порядковый номер в имени файла и в колонке `order` местами разошлись.

Записи идут по одной, без общей транзакции: обрыв на тридцатом гигабайте не должен
откатывать всё уже сделанное. Повторный запуск продолжает с того же места — берутся
только записи с пустым полем `file`.

    manage.py import_legacy_files --db D:/knt-legacy/db.sqlite3 --source D:/knt-media/inbicst
    manage.py import_legacy_files --db ... --source ... --apply
"""

import re
import sqlite3
import time
from pathlib import Path

from django.core.files import File as DjangoFile
from django.core.management.base import BaseCommand, CommandError

from attachments.models import File, file_upload_to

EXTENSION_MAX = 5
STEP = 50  # через сколько файлов отчитываться о ходе

# Старый сайт жил на линуксе, и в названиях осели знаки, которых windows в именах файлов
# не терпит: «Том 1: …», «Билет 1 | 21 дек.», кавычки. Чистим всегда, а не только на
# разработке: ключ обязан совпадать на диске и в бакете, значит и правило одно на оба.
FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def extension(path):
    """Расширение файла на диске — если это вообще расширение.

    У части записей точка стоит внутри названия («Порай-Кошиц М.А. Основы…»), и суффикс
    выходит длиной в полстроки. Такой в ключ пускать нельзя: по расширению выбирается
    значок, а random_key ещё и режет его по живому.
    """
    suffix = path.suffix.lstrip(".").lower()
    return suffix if suffix.isalnum() and len(suffix) <= EXTENSION_MAX else ""


def filename(name, suffix):
    """Имя внутри ключа: название записи плюс настоящее расширение.

    Название чаще всего уже кончается тем же расширением — второй раз не приписываем,
    иначе в хранилище копятся ключи вида «Матан.pdf.pdf». Хвостовые точки и пробелы
    windows молча срезает — уберём сами, чтобы имя на диске совпало с записанным в базу.
    """
    if suffix and name.lower().endswith(f".{suffix}"):
        name = name[: -len(suffix) - 1]
    stem = re.sub(r"\s+", " ", FORBIDDEN.sub(" ", name)).strip(" .") or "file"
    return f"{stem}.{suffix}" if suffix else stem


def human(size):
    return f"{size / 1024 ** 3:.1f} ГиБ"


class Command(BaseCommand):
    help = "Кладёт в хранилище блобы для записей File, оставшихся без файла."

    def add_arguments(self, parser):
        parser.add_argument("--db", required=True, help="db.sqlite3 старого сайта")
        parser.add_argument("--source", required=True, help="каталог с выгрузкой файлов")
        parser.add_argument("--limit", type=int, help="взять только первые N записей — для примерки")
        parser.add_argument("--apply", action="store_true", help="без него только показывает, что перенёс бы")

    def handle(self, *args, **options):
        old_db = Path(options["db"])
        source = Path(options["source"])
        self.apply = options["apply"]
        if not old_db.is_file():
            raise CommandError(f"нет файла базы: {old_db}")
        if not source.is_dir():
            raise CommandError(f"нет каталога с выгрузкой: {source}")

        links = self.links(old_db)
        storage = File._meta.get_field("file").storage

        records = list(File.objects.filter(file="").order_by("pk"))
        if options["limit"]:
            records = records[: options["limit"]]
        planned = sum(record.size or 0 for record in records)
        self.stdout.write(f"записей без файла: {len(records)} · {human(planned)}")

        started = time.monotonic()
        moved = size = 0
        lost, absent = [], []
        for record in records:
            link = links.get(record.pk)
            if not link:
                lost.append(record.pk)
                continue
            blob = source / link.rsplit("/", 1)[-1]
            if not blob.is_file():
                absent.append(blob.name)
                continue

            # Размер берём с диска: в старой базе он лежал строкой «32.50 MB» и после
            # разбора расходится с настоящим на пару килобайт.
            actual = blob.stat().st_size
            key = file_upload_to(record, filename(record.name, extension(blob)))
            if self.apply:
                with blob.open("rb") as stream:
                    record.file = storage.save(key, DjangoFile(stream))
                record.size = actual
                record.save(update_fields=["file", "size"])

            moved += 1
            size += actual
            if moved % STEP == 0:
                self.tick(moved, len(records), size, planned, time.monotonic() - started)

        self.report(moved, size, lost, absent)

    def tick(self, moved, total, size, planned, spent):
        """Ход работы. Со сбросом буфера: перенаправленный в файл stdout копится блоками,
        и без этого строки выпадали бы пачками раз в полтора гигабайта — то есть прогресс
        не видно ровно тогда, когда он нужен."""
        line = f"  {moved}/{total} · {human(size)} из {human(planned)}"
        if self.apply and size:
            left = (planned - size) / (size / spent)
            line += f" · осталось ~{left / 60:.0f} мин"
        self.stdout.write(line)
        self.stdout.flush()

    def links(self, old_db):
        """id записи → адрес файла на старом сервере."""
        old = sqlite3.connect(f"file:{old_db}?mode=ro", uri=True)
        try:
            rows = old.execute("select id, remote_link from system_file").fetchall()
        finally:
            old.close()
        return {pk: link for pk, link in rows if link}

    def report(self, moved, size, lost, absent):
        self.stdout.write(f"  перенесено: {moved} · {human(size)}")
        if lost:
            self.stderr.write(f"нет ссылки в старой базе: {len(lost)} — {lost[:10]}")
        if absent:
            self.stderr.write(f"нет файла в выгрузке: {len(absent)} — {absent[:10]}")
        if self.apply:
            self.stdout.write(self.style.SUCCESS(f"осталось без файла: {File.objects.filter(file='').count()}"))
        else:
            self.stdout.write(self.style.WARNING("пробный прогон, ничего не записано — запусти с --apply"))
