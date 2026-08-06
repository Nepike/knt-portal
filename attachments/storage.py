from django.conf import settings
from django.core.files.storage import FileSystemStorage


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
