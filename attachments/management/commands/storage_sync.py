"""Перенос блобов между локальным диском и R2.

Полной копии хранилища мы НЕ держим. Команда нужна, когда режим переключали:
файлы, загруженные пока R2 был недоступен, лежат на диске, и их надо догнать руками.

Ключ у блоба в обоих хранилищах один и тот же — books/<uuid>/Зорич.pdf. Префикс
бакета (R2_PREFIX) добавляет сам S3Storage, в базе его нет. Поэтому перенос —
это копия под тем же именем, и в базе менять нечего.

    manage.py storage_sync --check           какие записи не находят свой файл
    manage.py storage_sync --push --apply    локальный диск → R2
    manage.py storage_sync --pull --apply    R2 → локальный диск
"""

import time

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.management.base import BaseCommand, CommandError

from attachments.storage import media_fields

STEP = 25  # через сколько файлов отчитываться о ходе


def human(size):
    return f"{size / 1024 ** 3:.1f} ГиБ"


def remote_storage():
    # file_overwrite=True только здесь: боевое хранилище нарочно не перезаписывает
    # одноимённый объект, а нам нужно положить блоб ровно под тем ключом, что в базе.
    if not settings.R2_BUCKET:
        raise CommandError("R2 не настроен (пустой R2_BUCKET) — переносить некуда.")

    from storages.backends.s3 import S3Storage

    return S3Storage(**{**settings.R2_OPTIONS, "file_overwrite": True})


def already_there(storage):
    """Проверка «этот ключ в приёмнике уже есть».

    У бакета спрашиваем одним листингом, а не по объекту: HEAD на каждый из пяти тысяч
    блобов тянет дольше, чем сама заливка, и платить за это приходится при КАЖДОМ
    повторном запуске — то есть ровно тогда, когда докачиваешь оборвавшееся.
    """
    if isinstance(storage, FileSystemStorage):
        return storage.exists

    pages = storage.connection.meta.client.get_paginator("list_objects_v2")
    prefix = storage.location.strip("/")
    cut = len(prefix) + 1 if prefix else 0
    keys = {
        obj["Key"][cut:]
        for page in pages.paginate(Bucket=storage.bucket_name, Prefix=prefix)
        for obj in page.get("Contents", [])
    }
    return keys.__contains__


def blobs():
    """(модель, поле, ключ) по всем файловым полям проекта."""
    for model, field_name in media_fields():
        rows = model.objects.exclude(**{field_name: ""}).exclude(**{f"{field_name}__isnull": True})
        for row in rows.iterator():
            name = getattr(row, field_name).name
            if name:
                yield model, field_name, name


class Command(BaseCommand):
    help = "Переносит файлы между локальным диском и R2. Без --apply только показывает."

    def add_arguments(self, parser):
        what = parser.add_mutually_exclusive_group(required=True)
        what.add_argument("--check", action="store_true", help="записи без файла в активном хранилище")
        what.add_argument("--push", action="store_true", help="локальный диск → R2")
        what.add_argument("--pull", action="store_true", help="R2 → локальный диск")
        parser.add_argument("--apply", action="store_true", help="без него только показывает")

    def handle(self, *args, **options):
        if options["check"]:
            return self.check_active()

        local, remote = FileSystemStorage(), remote_storage()
        source, target = (local, remote) if options["push"] else (remote, local)
        self.copy(source, target, options["apply"])

    def check_active(self):
        """Хранилище берём у самого поля: именно оттуда сайт будет забирать файл."""
        missing = 0
        for model, field_name, name in blobs():
            if not model._meta.get_field(field_name).storage.exists(name):
                missing += 1
                self.stdout.write(f"нет файла: {model.__name__}.{field_name} → {name}")
        self.stdout.write(self.style.SUCCESS(f"записей без файла: {missing}"))

    def copy(self, source, target, apply):
        plan = list(blobs())
        done = already_there(target)
        # Считаем от работы ЭТОГО запуска, а не от всей базы: продолжая оборванную заливку,
        # иначе смотришь, как счётчик замирает недобрав до общего объёма, и решаешь, что
        # опять оборвалось.
        todo = [item for item in plan if not done(item[2])]
        # Сколько всего байт — только когда читаем с диска: у бакета размер каждого объекта
        # это отдельный запрос, и ради полоски прогресса вышли бы тысячи лишних обращений.
        planned = self.weigh(source, todo) if isinstance(source, FileSystemStorage) else 0
        self.stdout.write(
            f"файлов в базе: {len(plan)}"
            + (f" · уже в приёмнике: {len(plan) - len(todo)}" if len(todo) < len(plan) else "")
            + f" · переносим: {len(todo)}"
            + (f" · {human(planned)}" if planned else "")
        )

        started = time.monotonic()
        moved = lost = size = 0
        for model, field_name, name in todo:
            if not source.exists(name):
                lost += 1
                continue
            if not apply:
                self.stdout.write(f"{model.__name__}.{field_name}: {name}")
                continue

            size += source.size(name)
            with source.open(name) as blob:
                target.save(name, blob)  # файловый объект, а не байты: сканы бывают на сотни МБ
            moved += 1
            if moved % STEP == 0:
                self.tick(moved, size, planned, time.monotonic() - started)

        if lost:
            self.stderr.write(f"нет ни там, ни там: {lost}")
        verdict = f"перенесено: {moved} · {human(size)}" if apply else "показано выше (запусти с --apply)"
        self.stdout.write(self.style.SUCCESS(verdict))

    def weigh(self, storage, plan):
        return sum(storage.size(name) for _, _, name in plan if storage.exists(name))

    def tick(self, moved, size, planned, spent):
        """Со сбросом буфера: перенаправленный в файл stdout копится блоками, и без этого
        строки выпадали бы пачками — то есть прогресс не видно ровно тогда, когда он нужен."""
        line = f"  {moved} · {human(size)}"
        if planned:
            left = (planned - size) / (size / spent)
            line += f" из {human(planned)} · осталось ~{left / 60:.0f} мин"
        self.stdout.write(line)
        self.stdout.flush()
