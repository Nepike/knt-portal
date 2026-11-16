"""Спека: что считается готовым медиафайлом. Договор — `docs/media-pipeline.md`.

Сайт не конвертирует: файл печётся снаружи, а здесь проверяется. Числа отдаёт ручка
`GET /intake/spec/`, и пекарня сверяет результат теми же функциями — проверка одна.
Команду ffmpeg сайт не отдаёт: кодек выбирается на месте (NVENC или libx264).

Без Django: модуль берёт с собой `tools/bake.py` (на это есть тест).

* `Clip` — один короткий файл (косметика): размер, длина, вес.
* `Ladder` — набор HLS (лекция): меряется описание набора, а не файлы.
"""

from collections import namedtuple
from math import ceil

MB = 1024 * 1024

# Заглавные файлы набора HLS: пекарня их пишет, сайт по ним находит набор по префиксу.
MASTER = "master.m3u8"
POSTER = "poster.jpg"

# width/height — точный размер: видео печётся с нуля по этой строке. У картинок мерка
# мягче (`cosmetics/specs.py`) — намеренно: форма админки принимает и сделанное руками.
# poster_bytes — потолок постера: он качается на каждом показе витрины.
Clip = namedtuple("Clip", "title width height seconds max_bytes audio poster_bytes")

# heights — ПОТОЛОК лесенки, сверху вниз: вверх не растягиваем (из 720p — одна дорожка).
# Потолка веса у лекции нет: двухчасовую запись одним числом не ограничить.
Ladder = namedtuple("Ladder", "title heights fps segment audio_kbps poster_bytes")

SHAPES = {Clip: "clip", Ladder: "ladder"}
KINDS = {name: kind for kind, name in SHAPES.items()}

RECIPES = {
    "cosmetic-header": Clip("шапка профиля", 1536, 256, 8, 2 * MB, audio=False, poster_bytes=MB // 2),
    "cosmetic-background": Clip("фон профиля", 1920, 1080, 8, 8 * MB, audio=False, poster_bytes=MB),
    # Две ступени: место дороже промежуточного качества. 30 кадров — потолок: доске с мелом
    # шестьдесят не нужны. Сегмент 6 с — для записи, которую смотрят подряд.
    "lecture": Ladder("лекция", heights=(1080, 720), fps=30, segment=6, audio_kbps=128, poster_bytes=MB),
}


def payload():
    """Спека словарём — для JSON и пекарни. `shape` явно: угадывать вид по ключам ненадёжно."""
    return {name: {"shape": SHAPES[type(r)], **r._asdict()} for name, r in RECIPES.items()}


def recipe_from(data):
    """Рецепт из того, что прислал сайт. Незнакомое поле — пекарня отстала от репозитория:
    печь по половине требований хуже, чем не печь."""
    data = dict(data)
    shape = data.pop("shape", None)
    kind = KINDS.get(shape)
    if kind is None:
        raise ValueError(f"пекарня не знает такого вида рецепта: {shape!r}. Обнови репозиторий")

    unknown = sorted(set(data) - set(kind._fields))
    if unknown:
        raise ValueError(f"сайт прислал поля, которых пекарня не знает: {', '.join(unknown)}. Обнови репозиторий")
    missing = sorted(set(kind._fields) - set(data))
    if missing:
        raise ValueError(f"сайт не прислал: {', '.join(missing)}")

    if kind is Ladder:
        data["heights"] = tuple(data["heights"])  # в JSON кортежей нет, а сравнивать надо
    return kind(**data)


def check(recipe, info, size):
    """Причина отказа по одиночному файлу или None (`info` — от `mp4.probe`). Отказ говорит,
    ЧТО перепечь."""
    if size > recipe.max_bytes:
        return f"тяжелее {recipe.max_bytes // MB} МБ ({size / MB:.1f} МБ)"
    if not info["faststart"]:
        # moov в конце играть не мешает, но это лишний круг до первого кадра и лишний МБ
        # у nginx. Чинится ремуксом без перекодировки — отказываем.
        return "нет faststart: оглавление в конце файла. Перепеки с -movflags +faststart"
    if info["audio"] and not recipe.audio:
        # Пояснение верно, пока рецепты без звука — только косметика.
        return "звуковая дорожка лишняя — это фон под текстом, перепеки с -an"
    if (info["width"], info["height"]) != (recipe.width, recipe.height):
        return f"ждали {recipe.width}×{recipe.height}, приехало {info['width']}×{info['height']}"
    if info["seconds"] > recipe.seconds:
        return f"длиннее {recipe.seconds} с ({info['seconds']:.1f} с)"
    return None


def check_ladder(ladder, made):
    """Причина отказа по готовому набору HLS или None.

    `made` — описание: `{"duration": секунды, "poster_bytes": N,
    "renditions": [{"height": 1080, "width": 1920, "segments": N, "bytes": N}, …]}`.
    Проверяем описание, а не тысячи файлов; число сегментов сверяется с длительностью —
    так ловится оборванная выпечка.
    """
    made_heights = [r["height"] for r in made["renditions"]]
    if not made_heights:
        return "ни одной дорожки"
    if len(set(made_heights)) != len(made_heights):
        return f"дорожки повторяются: {sorted(made_heights)}"
    if (tallest := max(made_heights)) > (top := max(ladder.heights)):
        return f"дорожка {tallest}p выше верхней ступени {top}p"
    if made["duration"] <= 0:
        return "нулевая длительность"

    # Последний сегмент неполный — округляем вверх; разбежка в один: ffmpeg режет по опорным кадрам.
    expected = ceil(made["duration"] / ladder.segment)
    for rendition in made["renditions"]:
        if abs(rendition["segments"] - expected) > 1:
            return (f"у дорожки {rendition['height']}p {rendition['segments']} сегментов, "
                    f"а по длительности {made['duration']:.0f} с ждали около {expected}")
    if made["poster_bytes"] > ladder.poster_bytes:
        return f"обложка тяжелее {ladder.poster_bytes // MB} МБ ({made['poster_bytes'] / MB:.1f} МБ)"
    return None
