"""Чтение заголовка mp4: размеры, длительность, faststart, есть ли звук.

Это разбор КОНТЕЙНЕРА, а не обработка видео: читаем дерево коробок в начале файла
и достаём несколько чисел. Ни ffmpeg, ни декодирования — сайт по договору
(`docs/media-pipeline.md`) файлы не трогает, только проверяет.

Зачем вообще: ошибку «забыли faststart» не видит тот, кто загружал, — у него файл
уже на диске. Её замечают студенты, у которых видео не начинается, пока не скачается
целиком. Проверить это можно только тут.
"""

import struct

# Сколько байт от начала читаем. Если `moov` дальше — файл и так без faststart,
# а значит уже негоден, и дочитывать его незачем.
HEAD = 2 * 1024 * 1024

# Коробки, внутрь которых надо заходить: у остальных содержимое — данные, а не дерево.
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


class Broken(Exception):
    """Файл не разбирается: либо не mp4, либо обрезан."""


def _boxes(data, start=0, end=None):
    """Коробки одного уровня: (тип, начало содержимого, конец)."""
    end = len(data) if end is None else end
    while start + 8 <= end:
        size, kind = struct.unpack(">I4s", data[start:start + 8])
        head = 8
        if size == 1:  # 64-битный размер лежит следом
            if start + 16 > end:
                return
            size = struct.unpack(">Q", data[start + 8:start + 16])[0]
            head = 16
        elif size == 0:  # «до конца файла»
            size = end - start
        if size < head:
            return
        yield kind, start + head, min(start + size, end)
        start += size


def _walk(data, start=0, end=None):
    """Все коробки дерева вглубь — пар (тип, начало, конец) хватает для наших полей."""
    for kind, body, stop in _boxes(data, start, end):
        yield kind, body, stop
        if kind in CONTAINERS:
            yield from _walk(data, body, stop)


def _fixed(value):
    """16.16 с фиксированной точкой — так mp4 хранит ширину и высоту."""
    return value / 65536


def probe(handle):
    """Что известно о файле: width, height, seconds, faststart, audio.

    Ждёт открытый двоичный файл. Читает только начало.
    """
    handle.seek(0)
    data = handle.read(HEAD)
    if len(data) < 8 or data[4:8] != b"ftyp":
        raise Broken("это не mp4")

    # faststart — это когда оглавление (`moov`) лежит ДО самих данных (`mdat`).
    order = [kind for kind, _, _ in _boxes(data) if kind in (b"moov", b"mdat")]
    if b"moov" not in order:
        raise Broken("оглавление не в начале файла")
    faststart = order[0] == b"moov"

    width = height = 0
    seconds = 0.0
    audio = False
    for kind, body, stop in _walk(data):
        if kind == b"smhd":
            audio = True
        elif kind == b"mvhd":
            version = data[body]
            shift = body + (20 if version else 12)
            scale, length = struct.unpack(">IQ" if version else ">II", data[shift:shift + (12 if version else 8)])
            seconds = length / scale if scale else 0.0
        elif kind == b"tkhd":
            # Ширина и высота лежат в самом конце tkhd, за матрицей преобразования:
            # 4 версия+флаги, 20 (или 32 у версии 1) времена и id, 8 запас, 8 слой
            # и громкость, 36 матрица. Отсюда 76 и 88.
            version = data[body]
            shift = body + (88 if version else 76)
            if shift + 8 <= stop:
                raw_width, raw_height = struct.unpack(">II", data[shift:shift + 8])
                # Звуковая дорожка тоже tkhd, но у неё размеры нулевые.
                width = max(width, int(_fixed(raw_width)))
                height = max(height, int(_fixed(raw_height)))

    return {"width": width, "height": height, "seconds": seconds, "faststart": faststart, "audio": audio}
