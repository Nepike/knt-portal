"""Адреса пользовательских файлов: наш адрес с подписью, а откуда байты (R2 или диск) —
решает вьюха. Подпись R2 менялась бы на каждом рендере и убивала кеш браузера.
"""

from pathlib import PurePosixPath

from django.conf import settings
from django.core import signing
from django.core.files.storage import FileSystemStorage
from django.urls import reverse

MEDIA_SALT = "attachments.media"
FILE_SALT = "attachments.download"
# Своя соль у кусков HLS: разрешение на сегмент лекции не годится для других ключей.
HLS_SALT = "attachments.hls"
# Сколько браузер держит редирект там, где nginx не участвует (разработка).
MEDIA_CACHE = 3600


def _signer(salt):
    # Signer, не TimestampSigner: подпись одинакова на каждом рендере — работает кеш браузера.
    return signing.Signer(salt=salt)


def _external(path):
    return f"{settings.FILES_BASE_URL}{path}" if settings.FILES_BASE_URL else path


def _pretty(name):
    """Хвост адреса файла: из него браузер берёт имя при сохранении."""
    return name.replace("/", "_").replace("\\", "_").strip() or "file"


def redirect_url(key):
    """Наш адрес картинки, за которым прячется хранилище."""
    return reverse("media_image", args=[_signer(MEDIA_SALT).sign_object(key, compress=True)])


def image_url(key):
    """Адрес картинки по ключу — там, где за ней нет поля модели (обложка лекции)."""
    return _external(redirect_url(key))


def media_url(field):
    """Адрес картинки для разметки. Пустое поле — пустая строка."""
    if not field:
        return ""
    # На проде — через нашу вьюху: адрес один для R2 и диска, байты отдаёт nginx.
    if settings.FILES_BASE_URL or settings.MEDIA_ACCEL:
        return _external(redirect_url(field.name))
    # В dev локальный диск отдаёт постоянный адрес сам.
    if isinstance(field.storage, FileSystemStorage):
        return field.url
    return redirect_url(field.name)


def media_key(token):
    """Ключ в хранилище или None, если подпись не наша."""
    try:
        return _signer(MEDIA_SALT).unsign_object(token)
    except signing.BadSignature:
        return None


def file_url(file):
    """Адрес файла книги или материала. Подпись — и есть разрешение: на домене файлов нет
    сессии. Перебор не выйдет: токен подписан секретом сайта, в ключе uuid, листинг закрыт."""
    token = _signer(FILE_SALT).sign_object(file.pk)
    return _external(reverse("file_download", args=[token, _pretty(file.name)]))


def file_pk(token):
    """Номер файла или None, если подпись не наша."""
    try:
        return _signer(FILE_SALT).unsign_object(token)
    except signing.BadSignature:
        return None


def hls_url(key):
    """Наш адрес куска HLS. Имя в хвосте — ради расширения для плееров и кешей; ключ — в подписи."""
    token = _signer(HLS_SALT).sign_object(key, compress=True)
    return _external(reverse("hls_piece", args=[token, PurePosixPath(key).name]))


def hls_key(token):
    """Ключ куска HLS или None, если подпись не наша."""
    try:
        return _signer(HLS_SALT).unsign_object(token)
    except signing.BadSignature:
        return None
