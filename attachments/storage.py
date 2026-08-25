from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.files.storage import FileSystemStorage


# Длина FileField по умолчанию. Ключ обязан влезать: иначе запись не сохранится вовсе,
# а хранилище молча урезало бы имя и разошлось с тем, что записано в базе.
KEY_MAX = 100


def random_key(folder, filename):
    """Ключ с непредсказуемым сегментом: по адресу вида materials/12/images/foto.jpg
    библиотеку можно было бы перебрать, ни разу не зайдя на сайт. Имя файла оставляем
    в конце — с ним понятнее и в бакете, и при сохранении.

    Длинное имя подрезаем: под скачивание идёт File.name, а не хвост ключа (см. media._pretty),
    так что теряется только читаемость в бакете.
    """
    prefix = f"{folder}/{uuid4().hex}/"
    name = Path(filename).name
    suffix = Path(name).suffix[:20]  # расширение бережём целиком, по нему выбирается значок
    stem = name[: len(name) - len(suffix)][: max(KEY_MAX - len(prefix) - len(suffix), 1)]
    return f"{prefix}{stem or 'file'}{suffix}"


def media_storage():
    """Хранилище файлов книг и материалов: R2, если он настроен, иначе локальный диск.

    Django зовёт это один раз при импорте моделей, поэтому переключение — рестарт.
    Callable, а не готовый объект: миграция запоминает ссылку на функцию, и dev с prod
    живут на одной миграции с разными хранилищами.
    """
    if not settings.R2_BUCKET:
        return FileSystemStorage()

    from storages.backends.s3 import S3Storage

    return S3Storage(**settings.R2_OPTIONS)


def file_storage():
    """Хранилище, которым реально живёт File.file.

    Не media_storage() заново: объект выбран при импорте моделей, и в тестах он
    подменён на временный каталог (core/test_runner.py) — иначе тесты полезли бы в R2.
    """
    from .models import File

    return File._meta.get_field("file").storage


def media_fields():
    """Все файловые поля проекта. Проходим по всем моделям, а не по списку: новое поле
    однажды появится, а про команды и подмену хранилища в тестах забудут."""
    from django.apps import apps
    from django.db import models

    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, models.FileField):
                yield model, field.name


_BLOB_FIELDS = {}


def _drop_blobs(sender, instance, **kwargs):
    for name in _BLOB_FIELDS[sender]:
        blob = getattr(instance, name)
        if blob:
            blob.delete(save=False)


def connect_blob_cleanup():
    """Снимает блоб вместе с записью: иначе в бакете копятся сироты, на которые уже
    ниоткуда не сослаться. Вешаем на все файловые поля разом — фото и картинки
    комментариев живут в чужих приложениях, и про них тут забыли бы первыми.

    Зовётся из AttachmentsConfig.ready(), когда все модели уже загружены.
    """
    from django.db.models.signals import post_delete

    _BLOB_FIELDS.clear()
    for model, name in media_fields():
        _BLOB_FIELDS.setdefault(model, []).append(name)
    for model in _BLOB_FIELDS:
        post_delete.connect(_drop_blobs, sender=model, dispatch_uid="attachments.blobs")
