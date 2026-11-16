"""Палитра Стены.

Цвет хранится индексом в этом списке, по байту на клетку: с открытия доски список только
дополняется — перестановка перекрасила бы все пиксели и архивные доски.

Сетка «12 тонов × 8 светлот» в OKLCH: восемь ступеней — чтобы рисовать лесенкой (тень,
основной, блик). Насыщенность в строке одна на все тона, по строкам — горбом; сетка наклонена,
тёплая половина светлее (иначе тёмный жёлтый — болото); углы — под названия цветов.
Нейтральные — отдельным рядом, тёплые, без середины: середина — сам бетон, его получают
стиранием. Значения проверены на попадание в sRGB.
"""

import math
from collections import namedtuple

Color = namedtuple("Color", "code hex name")

EMPTY = 0
CONCRETE = "#95999C"

TONES = ["ночной", "тёмный", "глубокий", "основной", "яркий", "светлый", "бледный", "туманный"]
HUES = [
    "красный", "оранжевый", "янтарный", "жёлтый", "зелёный", "изумрудный",
    "бирюзовый", "голубой", "синий", "фиолетовый", "пурпурный", "розовый",
]
GRID = [
    ["#472724", "#4B301D", "#473719", "#3E3C1B", "#293E25", "#163B2D",
     "#013434", "#022C39", "#14223A", "#241D37", "#311C32", "#3E202C"],
    ["#693733", "#6B4224", "#654B1C", "#57531F", "#385831", "#1A553F",
     "#004D4D", "#004357", "#20375C", "#3A2F58", "#4D2C4F", "#5D2F42"],
    ["#8A4944", "#8C562F", "#836224", "#716B27", "#497240", "#217054",
     "#006767", "#005C75", "#304D7E", "#514279", "#693F6C", "#7D415A"],
    ["#A95E59", "#AA6D41", "#9F7A35", "#8A8539", "#5E8D54", "#348B6B",
     "#008283", "#077695", "#43659D", "#695997", "#855488", "#9B5672"],
    ["#C37973", "#C4875D", "#B99453", "#A49F57", "#78A66E", "#55A584",
     "#329C9C", "#388FAE", "#5D7FB6", "#8272B0", "#9E6EA1", "#B5708B"],
    ["#D89791", "#DAA480", "#D0B07A", "#BDB97D", "#97BF8F", "#7BBDA0",
     "#63B5B4", "#63A8C3", "#7B99C9", "#9A8EC4", "#B58AB7", "#CB8DA5"],
    ["#E9B7B2", "#ECC3A8", "#E5CCA5", "#D6D4A7", "#B9D7B3", "#A4D4BE",
     "#93CCCB", "#8FC0D4", "#9CB3D8", "#B4ABD4", "#CAA9CB", "#DDAEBF"],
    ["#F7D8D5", "#FBE2D2", "#F8EAD2", "#F0EFD4", "#DDEFD9", "#CFEBDE",
     "#C1E3E2", "#BCD8E5", "#C0CEE5", "#CEC9E2", "#DDC9DE", "#ECCFD9"],
]
NEUTRALS = [
    ("#17130E", "тушь"), ("#322D27", "уголь"), ("#4F4943", "графит"), ("#6E6861", "камень"),
    ("#C0B9B2", "песок"), ("#D6D0C8", "холст"), ("#ECE5DD", "бумага"), ("#FCF6EE", "мел"),
]
# Чистые цвета для логотипов и акцентов — по одному на тон, в порядке HUES.
BRIGHT = [
    "#E8333A", "#F57C1F", "#FFB000", "#FFE11A", "#3DBE4A", "#00B386",
    "#00B8C4", "#1E9BF0", "#2F5BEA", "#7A3FE4", "#C23ACF", "#F0407F",
]


def rgb(value):
    """Цвет тремя числами — в таком виде его ждут и холст, и картинка."""
    return tuple(int(value[at:at + 2], 16) for at in (1, 3, 5))


def _build():
    palette = [Color(EMPTY, CONCRETE, "бетон")]
    for tone, row in zip(TONES, GRID):
        for hue, value in zip(HUES, row):
            palette.append(Color(len(palette), value, f"{tone} {hue}"))
    for value, name in NEUTRALS:
        palette.append(Color(len(palette), value, name))
    for hue, value in zip(HUES, BRIGHT):
        palette.append(Color(len(palette), value, f"чистый {hue}"))
    return palette


PALETTE = _build()
NEUTRAL_FROM = 1 + len(TONES) * len(HUES)
BRIGHT_FROM = NEUTRAL_FROM + len(NEUTRALS)
PICKABLE = PALETTE[1:]  # всё, кроме пустой клетки


def get(code):
    return PALETTE[code]


def _oklab(red, green, blue):
    """sRGB → OKLab: расстояние в нём близко к различию на глаз."""
    def straight(value):
        value /= 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (straight(value) for value in (red, green, blue))
    long = (0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue) ** (1 / 3)
    middle = (0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue) ** (1 / 3)
    short = (0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue) ** (1 / 3)
    return (
        0.2104542553 * long + 0.7936177850 * middle - 0.0040720468 * short,
        1.9779984951 * long - 2.4285922050 * middle + 0.4505937099 * short,
        0.0259040371 * long + 0.7827717662 * middle - 0.8086757660 * short,
    )


_LAB = [_oklab(*rgb(color.hex)) for color in PALETTE]

PULL = 0.15  # насколько важно попасть в ту же половину цветового круга


def nearest(red, green, blue):
    """Ближайший цвет палитры — для переноса картинок. Бетон — тоже кандидат: серый фон лучше
    оставить пустым. Штраф за чужой тон: иначе блёклые цвета уходили куда попало (тёплый
    коричневый — в бирюзу)."""
    light, warm, cool = _oklab(red, green, blue)
    chroma = math.hypot(warm, cool)

    def far(code):
        theirs = _LAB[code]
        gap = sum((mine - other) ** 2 for mine, other in zip(theirs, (light, warm, cool)))
        strength = math.hypot(theirs[1], theirs[2])
        if not chroma or not strength:
            return gap  # серому тон не важен, ему и приписать нечего
        turn = (theirs[1] * warm + theirs[2] * cool) / (chroma * strength)
        return gap + PULL * (1 - turn) * chroma

    return min(range(len(PALETTE)), key=far)

