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

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.management.base import BaseCommand, CommandError

from attachments.storage import media_fields


def remote_storage():
    # file_overwrite=True только здесь: боевое хранилище нарочно не перезаписывает
    # одноимённый объект, а нам нужно положить блоб ровно под тем ключом, что в базе.
    if not settings.R2_BUCKET:
        raise CommandError("R2 не настроен (пустой R2_BUCKET) — переносить некуда.")

    from storages.backends.s3 import S3Storage

    return S3Storage(**{**settings.R2_OPTIONS, "file_overwrite": True})


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
        moved = lost = 0
        for model, field_name, name in blobs():
            if target.exists(name):
                continue
            if not source.exists(name):
                lost += 1
                continue
            self.stdout.write(f"{model.__name__}.{field_name}: {name}")
            if not apply:
                continue
            with source.open(name) as blob:
                # Отдаём файловый объект, а не прочитанные байты: сканы бывают
                # на сотни мегабайт, и класть их целиком в память незачем.
                target.save(name, blob)
            moved += 1

        if lost:
            self.stderr.write(f"нет ни там, ни там: {lost}")
        verdict = f"перенесено: {moved}" if apply else "показано выше (запусти с --apply)"
        self.stdout.write(self.style.SUCCESS(verdict))
