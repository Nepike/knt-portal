import os
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import transaction


# Длина FileField по умолчанию: ключ обязан влезть, иначе запись не сохранится.
KEY_MAX = 100

# Тип содержимого по расширению — СВОЙ ЗАКРЫТЫЙ список, без mimetypes. Тип ложится в объект
# и доезжает до браузера; объявлять его должны одинаково сайт и пекарня, а mimetypes на
# Windows читает реестр и не знает `.m4s`. Только то, что браузер показывает, но не
# исполняет (не html, xml, svg); прочее — потоком байтов.
CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mp4": "video/mp4",
    ".m4s": "video/iso.segment",
    ".json": "application/json",  # пекарня кладёт свой manifest.json рядом с набором
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".apng": "image/apng",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".pdf": "application/pdf",
    # Офисные документы — для печати (printing.views.ACCEPTED): браузер их сохраняет.
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    # Без charset: кодировку конспекта десятилетней давности браузер угадает лучше нас.
    ".txt": "text/plain",
    ".c": "text/plain",
    ".h": "text/plain",
    ".cpp": "text/plain",
    ".py": "text/plain",
}


def content_type(name):
    """Тип для браузера из CONTENT_TYPES, иначе поток байтов: страница или svg с домена файлов
    исполнились бы в нём как код."""
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def random_key(folder, filename):
    """Ключ с uuid: иначе библиотеку перебрали бы по адресам. Имя файла — в конце, длинное
    подрезаем (при скачивании имя берётся из File.name)."""
    prefix = f"{folder}/{uuid4().hex}/"
    name = Path(filename).name
    suffix = Path(name).suffix[:20]  # расширение бережём целиком, по нему выбирается значок
    stem = name[: len(name) - len(suffix)][: max(KEY_MAX - len(prefix) - len(suffix), 1)]
    return f"{prefix}{stem or 'file'}{suffix}"


def media_storage():
    """R2, если настроен, иначе локальный диск. Зовётся при импорте моделей — смена означает
    рестарт. Callable: миграция помнит функцию, dev и prod живут на одной миграции."""
    if not settings.R2_BUCKET:
        return FileSystemStorage()

    from .r2 import R2Storage

    return R2Storage(**settings.R2_OPTIONS)


def file_storage():
    """Хранилище, которым живёт File.file: выбрано при импорте, в тестах подменено (core/test_runner.py)."""
    from .models import File

    return File._meta.get_field("file").storage


# Столько ключей S3 принимает в одном запросе на удаление.
BATCH = 1000


def _under(storage, folder):
    """Все ключи под папкой, вглубь. Пустая или несуществующая — пусто."""
    try:
        folders, files = storage.listdir(folder)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return
    for name in files:
        yield f"{folder}/{name}"
    for name in folders:
        yield from _under(storage, f"{folder}/{name}")


def _drop_empty_dirs(storage, folder):
    """Пустые каталоги на диске, снизу вверх: в бакете каталогов нет, а на диске они копятся."""
    try:
        folders, _ = storage.listdir(folder)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return
    for name in folders:
        _drop_empty_dirs(storage, f"{folder}/{name}")
    with suppress(OSError):  # не пуст — и пусть остаётся
        os.rmdir(storage.path(folder))


def drop_prefix(prefix):
    """Снять всё под префиксом (папка лекции — тысячи сегментов, а `post_delete` снимает только
    файловые поля). В S3 — пакетами по тысяче. Возвращает, сколько сняли."""
    storage = file_storage()
    prefix = prefix.strip("/")
    keys = list(_under(storage, prefix))
    if isinstance(storage, FileSystemStorage):
        for key in keys:
            storage.delete(key)
        _drop_empty_dirs(storage, prefix)
        return len(keys)

    # Ключ в бакете идёт с префиксом хранилища, а listdir отдаёт путь без него.
    location = f"{storage.location}/" if getattr(storage, "location", "") else ""
    client = storage.connection.meta.client
    for start in range(0, len(keys), BATCH):
        batch = [{"Key": location + key} for key in keys[start:start + BATCH]]
        client.delete_objects(Bucket=storage.bucket_name, Delete={"Objects": batch, "Quiet": True})
    return len(keys)


def media_fields():
    """Все файловые поля проекта — по всем моделям, чтобы не забыть новое поле."""
    from django.apps import apps
    from django.db import models

    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, models.FileField):
                yield model, field.name


_BLOB_FIELDS = {}


def _drop_blobs(sender, instance, **kwargs):
    """Снять блобы удалённой записи после коммита: откат транзакции блоб не вернёт. robust —
    сбой хранилища уходит в лог, а не в 500; сирота дешевле потерянного файла."""
    for name in _BLOB_FIELDS[sender]:
        blob = getattr(instance, name)
        if blob:
            transaction.on_commit(_dropper(blob.storage, blob.name), robust=True)


def _dropper(storage, key):
    """Функция, а не partial: при сбое robust-колбэка Django пишет в лог его __qualname__."""
    def drop_blob():
        storage.delete(key)
    return drop_blob


def connect_blob_cleanup():
    """Снимает блоб вместе с записью — на всех файловых полях разом. Зовётся из
    AttachmentsConfig.ready()."""
    from django.db.models.signals import post_delete

    _BLOB_FIELDS.clear()
    for model, name in media_fields():
        _BLOB_FIELDS.setdefault(model, []).append(name)
    for model in _BLOB_FIELDS:
        post_delete.connect(_drop_blobs, sender=model, dispatch_uid="attachments.blobs")
