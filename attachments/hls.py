"""Манифесты HLS: относительные имена кусков → наши подписанные адреса. Текст крошечный,
переписываем на лету. Договор — `docs/media-pipeline.md`.
"""

import posixpath
import re

from django.core.cache import cache

from .media import hls_url
from .storage import file_storage

# Тип, который ждёт hls.js; `application/x-mpegURL` — старое написание.
MANIFEST_TYPE = "application/vnd.apple.mpegurl"
# Двухчасовая лекция — ~1200 строк. Потолок — на случай, если по ключу не манифест.
MANIFEST_MAX = 4 * 1024 * 1024
# Куски неизменны (uuid, без перезаписи), подпись без времени — переписанный текст постоянен.
MANIFEST_CACHE = 24 * 3600

_URI_IN_TAG = re.compile(r'URI="([^"]*)"')


def _sibling(folder, uri):
    """Ключ соседнего куска. Подпись выдаётся на ЛЮБОЙ вычисленный ключ — `../` увёл бы её
    на чужой файл."""
    if uri.startswith(("/", "http://", "https://")) or ".." in uri.split("/"):
        raise ValueError(f"в манифесте посторонний адрес: {uri}")
    return posixpath.normpath(posixpath.join(folder, uri))


def _tag(folder, line):
    """Строка-тег: адрес бывает и в теге (`#EXT-X-MAP:URI="init_0.mp4"` — init-кусок)."""
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
