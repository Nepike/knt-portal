"""Стартовый набор шапок профиля.

Рамки достались от старого сайта готовыми, а шапок не было ни одной — этот набор
рисуется здесь, чтобы слоту было чем наполниться. Всё абстрактное: шапка лежит фоном
под именем и аватаром, и любой сюжет на ней мешал бы читать.

    manage.py make_headers            # показать, что нарисует
    manage.py make_headers --apply
    manage.py make_headers --refresh --apply   # перерисовать существующие

Свои картинки можно просто залить через админку — команда нужна только на старте.
"""

import math
import random
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from PIL import Image, ImageDraw, ImageFilter

from cosmetics.models import CosmeticItem

# Шапка тянется на всю ширину карточки профиля (768 точек) при высоте 128 — это 6:1.
# Рисуем вдвое крупнее ради экранов с удвоенной плотностью.
WIDTH, HEIGHT = 1536, 256
# Обложка для админки и витрин: та же картинка, только мельче.
COVER = (384, 64)

R = CosmeticItem.Rarity


def _mesh(colors, size=(4, 3)):
    """Плавная заливка из нескольких цветов.

    Приём вместо математики: рисуем крошечную картинку в несколько точек и растягиваем
    её бикубикой. Переходы получаются мягче любого рукописного градиента, а кода нет.
    """
    small = Image.new("RGB", size)
    small.putdata([random.choice(colors) for _ in range(size[0] * size[1])])
    return small.resize((WIDTH, HEIGHT), Image.BICUBIC)


def _glow(image, spots, colors, radius=200, alpha=90):
    """Мягкие световые пятна поверх заливки."""
    layer = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for _ in range(spots):
        x, y = random.randrange(WIDTH), random.randrange(HEIGHT)
        size = random.randrange(radius // 2, radius)
        draw.ellipse((x - size, y - size, x + size, y + size), fill=random.choice(colors))
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 3))
    return Image.blend(image, Image.blend(image, layer, 1), alpha / 255)


def _stripes(image, color, step=64, width=18, alpha=26):
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    for x in range(-HEIGHT, WIDTH + HEIGHT, step):
        draw.line((x, HEIGHT, x + HEIGHT, 0), fill=color, width=width)
    return Image.blend(image, layer, alpha / 100)


def _dots(image, color, step=32, size=2, alpha=35):
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    for y in range(step // 2, HEIGHT, step):
        for x in range(step // 2, WIDTH, step):
            draw.ellipse((x - size, y - size, x + size, y + size), fill=color)
    return Image.blend(image, layer, alpha / 100)


def _stars(image, count=260):
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    for _ in range(count):
        x, y = random.randrange(WIDTH), random.randrange(HEIGHT)
        size = random.choice((0, 0, 1, 1, 2))
        shade = random.randrange(180, 256)
        draw.ellipse((x - size, y - size, x + size, y + size), fill=(shade, shade, shade))
    return layer


def _grid(image, color, rows=9, alpha=55):
    """Уходящая к горизонту решётка — та самая, из восьмидесятых."""
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    horizon = HEIGHT // 3
    for step in range(1, rows + 1):
        y = horizon + (HEIGHT - horizon) * (step / rows) ** 2.2
        draw.line((0, y, WIDTH, y), fill=color, width=2)
    for step in range(-14, 15):
        draw.line((WIDTH / 2 + step * 26, horizon, WIDTH / 2 + step * 300, HEIGHT), fill=color, width=2)
    draw.rectangle((0, 0, WIDTH, horizon), fill=None)
    return Image.blend(image, layer, alpha / 100)


def _waves(image, color, count=5, alpha=45):
    layer = image.copy()
    draw = ImageDraw.Draw(layer)
    for wave in range(count):
        shift = wave * HEIGHT / count
        points = [
            (x, shift + math.sin(x / 190 + wave) * 26 + 30)
            for x in range(0, WIDTH + 8, 8)
        ]
        draw.line(points, fill=color, width=3, joint="curve")
    return Image.blend(image, layer, alpha / 100)


# Палитры под тёмную и светлую тему разом: шапка приглушена, поверх неё идёт текст.
NIGHT = [(15, 23, 42), (30, 41, 59), (51, 65, 85)]
VIOLET = [(46, 16, 101), (76, 29, 149), (109, 40, 217)]
OCEAN = [(8, 47, 73), (12, 74, 110), (3, 105, 161)]
SUNSET = [(124, 45, 18), (154, 52, 18), (194, 65, 12)]
FOREST = [(5, 46, 22), (20, 83, 45), (22, 101, 52)]
ROSE = [(76, 5, 25), (136, 19, 55), (159, 18, 57)]


def gradient_night():
    # NIGHT вдвое: иначе случайный выбор делает «Ночь» сплошь фиолетовой.
    return _mesh(NIGHT * 2 + VIOLET)


def gradient_ocean():
    return _mesh(OCEAN)


def stripes_sunset():
    return _stripes(_mesh(SUNSET), (255, 237, 213))


def dots_forest():
    return _dots(_mesh(FOREST), (187, 247, 208))


def waves_ocean():
    return _waves(_mesh(OCEAN), (186, 230, 253))


def bokeh_rose():
    return _glow(_mesh(ROSE), 9, [(251, 113, 133), (244, 63, 94), (253, 164, 175)])


def aurora():
    return _glow(_mesh(NIGHT), 7, [(34, 211, 238), (16, 185, 129), (124, 58, 237)], radius=260, alpha=120)


def starfield():
    return _stars(_mesh(NIGHT))


def retrowave():
    return _grid(_glow(_mesh(VIOLET + [(190, 24, 93)]), 4, [(236, 72, 153)]), (34, 211, 238))


def nebula():
    return _stars(_glow(_mesh(NIGHT + VIOLET), 10, [(124, 58, 237), (219, 39, 119), (37, 99, 235)], radius=300, alpha=140), count=340)


# Имя, редкость, рисовалка. Порядок тот же, в каком они лягут в инвентарь.
HEADERS = [
    ("Ночь", R.COMMON, gradient_night),
    ("Глубина", R.COMMON, gradient_ocean),
    ("Закат", R.COMMON, stripes_sunset),
    ("Хвоя", R.COMMON, dots_forest),
    ("Волны", R.RARE, waves_ocean),
    ("Лепестки", R.RARE, bokeh_rose),
    ("Сияние", R.EPIC, aurora),
    ("Звёздное поле", R.EPIC, starfield),
    ("Ретровейв", R.LEGENDARY, retrowave),
    ("Туманность", R.MYTHICAL, nebula),
]


class Command(BaseCommand):
    help = "Нарисовать стартовый набор шапок профиля"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="без него только показывает, что нарисует")
        parser.add_argument("--refresh", action="store_true", help="перерисовать уже существующие")
        parser.add_argument("--seed", type=int, default=20260820, help="зерно случайности: один и тот же даёт те же картинки")

    def handle(self, *args, **options):
        random.seed(options["seed"])
        done = 0
        for name, rarity, draw in HEADERS:
            item = CosmeticItem.objects.filter(source=f"generated:{name}").first()
            if item and not options["refresh"]:
                self.stdout.write(f"  уже есть: {name}")
                continue

            picture = draw().convert("RGB")
            body = _bytes(picture)
            cover = _bytes(picture.resize(COVER, Image.LANCZOS))
            self.stdout.write(
                f"  {name:16} {rarity:10} {len(body) // 1024:>4} КБ + {len(cover) // 1024} КБ обложка"
                f" · {'перерисовано' if item else 'нарисовано'}"
            )
            if options["apply"]:
                self.store(item or CosmeticItem(
                    name=name, rarity=rarity, source=f"generated:{name}",
                    kind=CosmeticItem.Kind.PROFILE_HEADER,
                ), name, body, cover)
            done += 1

        self.stdout.write(f"\nШапок: {done}")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Пробный прогон. Записать: --apply"))

    def store(self, item, name, body, cover):
        if item.pk:
            item.image.delete(save=False)
            item.still.delete(save=False)
        item.image.save(f"{name}.png", ContentFile(body), save=False)
        item.still.save(f"{name}-still.png", ContentFile(cover), save=False)
        item.save()


def _bytes(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
