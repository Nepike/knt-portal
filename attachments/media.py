"""Стабильные адреса картинок.

Подписанная ссылка R2 живёт час И меняется на каждом рендере (в подпись входит время).
Вклеенная прямо в HTML, она даёт сразу две беды: в открытой вкладке картинки протухают,
а браузер не может переиспользовать кеш — адрес каждый раз новый.

Поэтому в разметку идёт НАШ адрес с подписанным ключом: он постоянный, а свежую
ссылку хранилища выдаёт редирект (attachments.views.media_image).
"""

from django.core import signing
from django.urls import reverse

MEDIA_SALT = "attachments.media"
# Редирект браузер держит в кеше чуть меньше, чем живёт подпись R2 (querystring_expire),
# иначе он переиспользовал бы уже протухшую ссылку.
MEDIA_CACHE = 45 * 60


def _signer():
    # Signer, а не TimestampSigner: подпись должна быть ОДИНАКОВОЙ при каждом рендере,
    # иначе адрес снова поедет и кеш браузера снова окажется бесполезным.
    return signing.Signer(salt=MEDIA_SALT)


def media_url(field):
    """Адрес картинки для разметки. Пустое поле — пустая строка."""
    if not field:
        return ""
    return reverse("media_image", args=[_signer().sign_object(field.name, compress=True)])


def media_key(token):
    """Ключ в хранилище или None, если подпись не наша."""
    try:
        return _signer().unsign_object(token)
    except signing.BadSignature:
        return None
