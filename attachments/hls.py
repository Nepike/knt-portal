"""Манифесты HLS: относительные имена кусков → наши подписанные адреса.

Плеер читает манифест и идёт по именам, которые в нём написаны. Написаны они
относительными (`seg00042.m4s`), а у нас каждый адрес подписан — значит манифест
нельзя отдать как есть, его надо переписать. Файл текстовый и крошечный, так что
переписывается он на лету, а свойство «по бакету не походишь» сохраняется целиком.

Договор целиком — в `docs/media-pipeline.md`.
"""

import posixpath
import re

from django.core.cache import cache

from .media import hls_url
from .storage import file_storage

# Тип придумала Apple, и именно его ждёт hls.js; `application/x-mpegURL` — старое
# написание того же самого.
MANIFEST_TYPE = "application/vnd.apple.mpegurl"
# Манифест — это текст на несколько сотен килобайт в худшем случае (двухчасовая лекция
# даёт около 1200 строк). Потолок на случай, если по ключу вдруг окажется не манифест.
MANIFEST_MAX = 4 * 1024 * 1024
# Содержимое куска неизменно: в ключе uuid, перезапись в хранилище запрещена. Подпись
# у нас без времени, значит и переписанный текст постоянен — держать его можно долго.
# Иначе каждый запуск плеера стоил бы похода в хранилище и тысячи подписей.
MANIFEST_CACHE = 24 * 3600

_URI_IN_TAG = re.compile(r'URI="([^"]*)"')


def _sibling(folder, uri):
    """Ключ соседнего куска.

    Проверка не формальная: подпись выдаётся на ЛЮБОЙ вычисленный ключ, и `../` в имени
    увела бы её на чужую лекцию или вообще на чужой файл. Манифесты печём мы сами, но
    это ровно то место, где «мы сами» однажды перестаёт быть правдой.
    """
    if uri.startswith(("/", "http://", "https://")) or ".." in uri.split("/"):
        raise ValueError(f"в манифесте посторонний адрес: {uri}")
    return posixpath.normpath(posixpath.join(folder, uri))


def _tag(folder, line):
    """Строка-тег. Адрес бывает и в теге: `#EXT-X-MAP:URI="init_0.mp4"` — это init-кусок,
    без которого сегменты не декодируются вовсе."""
    found = _URI_IN_TAG.search(line)
    if not found:
        return line
    return line[:found.start(1)] + hls_url(_sibling(folder, found.group(1))) + line[found.end(1):]


def rewrite(key, text):
    """Манифест, в котором все имена заменены на наши подписанные адреса."""
    folder = posixpath.dirname(key)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
        elif stripped.startswith("#"):
            lines.append(_tag(folder, line))
        else:
            lines.append(hls_url(_sibling(folder, stripped)))
    return "\n".join(lines) + "\n"


def manifest(key):
    """Готовый текст манифеста по ключу. FileNotFoundError, если куска нет."""
    slot = f"hls:{key}"
    ready = cache.get(slot)
    if ready is None:
        with file_storage().open(key) as handle:
            ready = rewrite(key, handle.read(MANIFEST_MAX).decode("utf-8"))
        cache.set(slot, ready, MANIFEST_CACHE)
    return ready
