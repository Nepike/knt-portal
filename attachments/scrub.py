"""Снять с картинки всё, кроме самой картинки.

Снимок с телефона несёт в себе EXIF: модель телефона, время съёмки, часто координаты. В
анонимном отзыве о преподавателе или анонимном комментарии этого хватает, чтобы назвать
автора: геометка общежития сужает круг до комнаты. Чат и аватар пережимаются по пути и так,
отзыв, комментарий и галерея материала до 7 ноя 2026 сохранялись байт в байт.

Снимаем на уровне контейнера, а не пережатием: пиксели не трогаются вовсе, поэтому нет ни
потери качества, ни разжатия в память (снимок на 50 Мп развернулся бы в ~150 МБ на машине,
где их нет). Оставляем только то, без чего картинка выглядит иначе: цветовой профиль,
прозрачность, гамму — и поворот. Поворот телефон тоже пишет в EXIF, и без него снимок лёг бы
на бок, поэтому вместо прежнего EXIF ставится новый, с одним этим полем.

Чего не узнали — выбрасываем: от неизвестного блока проще отказаться, чем гадать, что в нём.
Всё, что после конца картинки, — тоже: туда дописывают и вторые кадры, и что угодно ещё.

Без Django: чистые байты на входе и на выходе.
"""

import struct
import zlib
from io import BytesIO

from PIL import Image

ORIENTATION = 0x0112


class Unreadable(ValueError):
    """Устроено не так, как обещает формат: такую картинку не принимаем вовсе."""


def scrub(data, kind):
    """Байты картинки без метаданных. kind — формат, как его назвал Pillow."""
    try:
        if kind in ("JPEG", "MPO"):
            return _jpeg(data, _orientation(data))
        if kind == "PNG":
            return _png(data, _orientation(data))
        if kind == "WEBP":
            return _webp(data, _orientation(data))
        if kind == "GIF":
            return _gif(data)
    except Unreadable:
        raise
    except (IndexError, ValueError, struct.error) as error:  # файл кончился посреди блока
        raise Unreadable(str(error)) from error
    raise Unreadable(f"формат {kind} не разбираем")


def _orientation(data):
    try:
        with Image.open(BytesIO(data)) as image:
            value = image.getexif().get(ORIENTATION, 1)
    except Exception:
        return 1
    return value if isinstance(value, int) and 2 <= value <= 8 else 1


def _tiff(orientation):
    """Новый EXIF с одним полем — поворотом. Без заголовка «Exif\\0\\0», как его ждут PNG и WebP."""
    exif = Image.Exif()
    exif[ORIENTATION] = orientation
    return exif.tobytes()[6:]


# --- JPEG ---------------------------------------------------------------------------------

def _keep_jpeg(marker, body):
    if 0xE0 <= marker <= 0xEF:  # APP0–APP15
        if marker == 0xE0:
            return True  # JFIF: плотность точек, без которой вид не меняется, — пусть остаётся
        if marker == 0xE2:
            return body.startswith(b"ICC_PROFILE\0")  # цвет; MPF (вторые кадры MPO) — нет
        if marker == 0xEE:
            return body.startswith(b"Adobe")  # без него CMYK-снимок раскрасится иначе
        return False  # EXIF и XMP (APP1), IPTC с автором (APP13) и прочие
    return marker != 0xFE  # COM — свободный текст


def _jpeg(data, orientation):
    if data[:2] != b"\xff\xd8":
        raise Unreadable("нет начала JPEG")
    out = [b"\xff\xd8"]
    if orientation != 1:
        exif = b"Exif\0\0" + _tiff(orientation)
        out.append(b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif)

    i = 2
    while True:
        if data[i] != 0xFF:
            raise Unreadable("в JPEG нет маркера там, где он должен быть")
        while data[i + 1] == 0xFF:  # заполняющие байты перед маркером
            i += 1
        marker = data[i + 1]
        if marker == 0xD9:  # конец картинки; дальше у MPO вторые кадры со своим EXIF
            out.append(b"\xff\xd9")
            return b"".join(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out.append(data[i:i + 2])
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if length < 2 or i + 2 + length > len(data):
            raise Unreadable("сегмент JPEG длиннее файла")
        if _keep_jpeg(marker, data[i + 4:i + 2 + length]):
            out.append(data[i:i + 2 + length])
        i += 2 + length

        if marker == 0xDA:  # за заголовком скана — сжатые данные до следующего маркера
            j = i
            while True:
                j = data.index(b"\xff", j)
                following = data[j + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7:
                    j += 2  # байт-заглушка или метка перезапуска — часть данных
                elif following == 0xFF:
                    j += 1  # заполняющий байт: маркер дальше
                else:
                    break
            out.append(data[i:j])
            i = j


# --- PNG ----------------------------------------------------------------------------------

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Кроме обязательных — только то, что меняет вид: прозрачность, гамма, цвет, анимация APNG.
# Текст (tEXt, zTXt, iTXt — там XMP и «автор»), eXIf, время (tIME) и чужие блоки уходят.
PNG_KEEP = {
    b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT", b"bKGD", b"pHYs",
    b"cICP", b"mDCV", b"cLLI", b"acTL", b"fcTL", b"fdAT",
}


def _png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png(data, orientation):
    if not data.startswith(PNG_SIGNATURE):
        raise Unreadable("нет подписи PNG")
    out = [PNG_SIGNATURE]
    i = len(PNG_SIGNATURE)
    while True:
        (length,) = struct.unpack(">I", data[i:i + 4])
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            raise Unreadable("блок PNG длиннее файла")
        # Заглавная первая буква — блок обязательный: без него картинку не прочитать.
        if kind in PNG_KEEP or kind[:1].isupper():
            out.append(data[i:end])
        if kind == b"IHDR" and orientation != 1:
            out.append(_png_chunk(b"eXIf", _tiff(orientation)))
        i = end
        if kind == b"IEND":
            return b"".join(out)


# --- WebP ---------------------------------------------------------------------------------

WEBP_KEEP = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}
VP8X_EXIF, VP8X_XMP = 0x08, 0x04


def _riff_chunk(kind, body):
    return kind + struct.pack("<I", len(body)) + body + (b"\0" if len(body) % 2 else b"")


def _webp(data, orientation):
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise Unreadable("нет заголовка WebP")
    (size,) = struct.unpack("<I", data[4:8])
    end = min(8 + size, len(data))
    chunks = []
    i = 12
    while i + 8 <= end:
        kind = data[i:i + 4]
        (length,) = struct.unpack("<I", data[i + 4:i + 8])
        body = data[i + 8:i + 8 + length]
        if len(body) != length:
            raise Unreadable("блок WebP длиннее файла")
        if kind in WEBP_KEEP:
            chunks.append([kind, body])
        i += 8 + length + length % 2

    extended = [chunk for chunk in chunks if chunk[0] == b"VP8X"]
    if extended:
        flags = extended[0][1][0] & ~(VP8X_EXIF | VP8X_XMP)
        if orientation != 1:
            flags |= VP8X_EXIF
            chunks.append([b"EXIF", _tiff(orientation)])  # EXIF — после данных картинки
        extended[0][1] = bytes([flags]) + extended[0][1][1:]
    # У простого WebP (без VP8X) метаданных не бывает, и поворот ему записать некуда.

    body = b"WEBP" + b"".join(_riff_chunk(kind, chunk) for kind, chunk in chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


# --- GIF ----------------------------------------------------------------------------------

# Из расширений-приложений нужны только счётчики повторов анимации; XMP и прочее — нет.
GIF_APPS = {b"NETSCAPE2.0", b"ANIMEXTS1.0"}


def _gif_blocks(data, i):
    """Конец цепочки подблоков, начинающейся с i (включая нулевой)."""
    while data[i]:
        i += 1 + data[i]
    return i + 1


def _gif(data):
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        raise Unreadable("нет заголовка GIF")
    packed = data[10]
    i = 13 + (3 * 2 ** ((packed & 7) + 1) if packed & 0x80 else 0)
    out = [data[:i]]
    while True:
        introducer = data[i]
        if introducer == 0x3B:  # конец
            out.append(b"\x3b")
            return b"".join(out)
        if introducer == 0x2C:  # кадр: описатель, своя палитра, данные
            packed = data[i + 9]
            start = i + 10 + (3 * 2 ** ((packed & 7) + 1) if packed & 0x80 else 0)
            end = _gif_blocks(data, start + 1)
            out.append(data[i:end])
            i = end
        elif introducer == 0x21:  # расширение
            label = data[i + 1]
            end = _gif_blocks(data, i + 2)
            app = data[i + 3:i + 3 + data[i + 2]] if label == 0xFF else b""
            # Комментарий (0xFE) и чужие приложения уходят; управление кадром и текст остаются.
            if label != 0xFE and (label != 0xFF or app in GIF_APPS):
                out.append(data[i:end])
            i = end
        else:
            raise Unreadable("в GIF неизвестный блок")
