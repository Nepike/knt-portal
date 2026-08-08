"""Адреса пользовательских файлов.

Наружу и картинка, и файл идут по нашему адресу с подписью, а откуда взять байты —
из R2 или с локального диска — решает уже вьюха. Поэтому переключение хранилища
не меняет ни одной ссылки в разметке.

Своё звено нужно ещё и потому, что подписанная ссылка R2 меняется на КАЖДОМ рендере
(в подпись входит время): вклеенная в HTML, она и протухает, и убивает кеш браузера.
"""

from django.conf import settings
from django.core import signing
from django.core.files.storage import FileSystemStorage
from django.urls import reverse

MEDIA_SALT = "attachments.media"
FILE_SALT = "attachments.download"
# Сколько браузер держит редирект там, где nginx не участвует (разработка).
MEDIA_CACHE = 3600


def _signer(salt):
    # Signer, а не TimestampSigner: подпись должна быть одинаковой при каждом рендере,
    # иначе адрес поедет и кеш браузера окажется бесполезным.
    return signing.Signer(salt=salt)


def _external(path):
    return f"{settings.FILES_BASE_URL}{path}" if settings.FILES_BASE_URL else path


def _pretty(name):
    """Хвост адреса файла: из него браузер берёт имя при сохранении."""
    return name.replace("/", "_").replace("\\", "_").strip() or "file"


def redirect_url(key):
    """Наш адрес картинки, за которым прячется хранилище."""
    return reverse("media_image", args=[_signer(MEDIA_SALT).sign_object(key, compress=True)])


def media_url(field):
    """Адрес картинки для разметки. Пустое поле — пустая строка."""
    if not field:
        return ""
    # На боевом сервере всё идёт через нашу вьюху: адрес один и тот же и для R2,
    # и для локального диска, а байты в обоих случаях отдаёт nginx.
    if settings.FILES_BASE_URL or settings.MEDIA_ACCEL:
        return _external(redirect_url(field.name))
    # В разработке локальный диск отдаёт постоянный адрес — лишнее звено ни к чему.
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
    """Адрес файла книги или материала.

    Подпись здесь и есть разрешение: домен файлов другой, куки сессии туда не приходят,
    проверять права во время скачивания нечем. Перебрать библиотеку это не даёт —
    токен подписан секретом сайта, в самом ключе uuid, листинг бакета закрыт.
    """
    token = _signer(FILE_SALT).sign_object(file.pk)
    return _external(reverse("file_download", args=[token, _pretty(file.name)]))


def file_pk(token):
    """Номер файла или None, если подпись не наша."""
    try:
        return _signer(FILE_SALT).unsign_object(token)
    except signing.BadSignature:
        return None
