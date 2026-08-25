"""Рисовалки для стартовой косметики: шапки и фоны профиля.

Своих картинок под эти слоты не было ни одной, а слот без содержимого не проверить.
Всё абстрактное и приглушённое намеренно: и шапка, и фон лежат ПОД текстом, и любой
сюжет на них мешал бы читать.

Размер приходит снаружи — шапка вытянутая, фон экранный, а приёмы одни и те же.
Кроме `mesh`, все берут размер у самой картинки.
"""

import math
import random
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter

# Палитры под обе темы разом: поверх идёт текст, поэтому всё тёмное и негромкое.
NIGHT = [(15, 23, 42), (30, 41, 59), (51, 65, 85)]
VIOLET = [(46, 16, 101), (76, 29, 149), (109, 40, 217)]
OCEAN = [(8, 47, 73), (12, 74, 110), (3, 105, 161)]
SUNSET = [(124, 45, 18), (154, 52, 18), (194, 65, 12)]
FOREST = [(5, 46, 22), (20, 83, 45), (22, 101, 52)]
ROSE = [(76, 5, 25), (136, 19, 55), (159, 18, 57)]


def mesh(size, colors, grid=(4, 3)):
    """Плавная заливка из нескольких цветов.

    Приём вместо математики: рисуем картинку в несколько точек и растягиваем бикубикой.
    Переходы мягче любого рукописного градиента, а кода нет.
    """
    small = Image.new("RGB", grid)
    small.putdata([random.choice(colors) for _ in range(grid[0] * grid[1])])
    return small.resize(size, Image.BICUBIC)


def glow(image, colors, spots=8, radius=200, alpha=90):
    """Мягкие световые пятна поверх заливки."""
    width, height = image.size
    layer = Image.new("RGB", image.size, (0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for _ in range(spots):
        x, y = random.randrange(width), random.randrange(height)
        size = random.randrange(radius // 2, radius)
        draw.ellipse((x - size, y - size, x + size, y + size), fill=random.choice(colors))
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 3))
    return Image.blend(image, Image.blend(image, layer, 1), alpha / 255)


def stripes(image, color, step=64, width=18, alpha=26):
    _, height = image.size
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    for x in range(-height, image.width + height, step):
        draw.line((x, height, x + height, 0), fill=color, width=width)
    return Image.blend(image, layer, alpha / 100)


def dots(image, color, step=32, size=2, alpha=35):
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    for y in range(step // 2, image.height, step):
        for x in range(step // 2, image.width, step):
            draw.ellipse((x - size, y - size, x + size, y + size), fill=color)
    return Image.blend(image, layer, alpha / 100)


def stars(image, count=260):
    width, height = image.size
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    for _ in range(count):
        x, y = random.randrange(width), random.randrange(height)
        size = random.choice((0, 0, 1, 1, 2))
        shade = random.randrange(180, 256)
        draw.ellipse((x - size, y - size, x + size, y + size), fill=(shade, shade, shade))
    return layer


def horizon(image, color, rows=9, alpha=55):
    """Уходящая к горизонту решётка — та самая, из восьмидесятых."""
    width, height = image.size
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    line = height // 3
    for step in range(1, rows + 1):
        y = line + (height - line) * (step / rows) ** 2.2
        draw.line((0, y, width, y), fill=color, width=2)
    for step in range(-14, 15):
        draw.line((width / 2 + step * (width // 59), line, width / 2 + step * (width // 5), height), fill=color, width=2)
    return Image.blend(image, layer, alpha / 100)


def waves(image, color, count=5, alpha=45, length=190, height=26):
    width, tall = image.size
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    for wave in range(count):
        shift = wave * tall / count
        points = [(x, shift + math.sin(x / length + wave) * height + 30) for x in range(0, width + 8, 8)]
        draw.line(points, fill=color, width=3, joint="curve")
    return Image.blend(image, layer, alpha / 100)


def png(image):
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
