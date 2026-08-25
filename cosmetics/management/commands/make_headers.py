"""Стартовые наборы шапок и фонов профиля.

Своих картинок под эти слоты не было ни одной, и слот без содержимого не проверить.
Рисовалки общие — `cosmetics/artwork.py`, здесь только рецепты и запись в базу.

    manage.py make_headers                       # показать, что нарисует
    manage.py make_headers --apply
    manage.py make_headers --kind background --apply
    manage.py make_headers --refresh --apply     # перерисовать существующие

Свои картинки можно просто залить через админку — команда нужна только на старте.
"""

import random

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from cosmetics import artwork as art
from cosmetics.models import CosmeticItem

R = CosmeticItem.Rarity
K = CosmeticItem.Kind

# Рисуем вдвое крупнее показа — ради экранов с удвоенной плотностью.
SIZES = {K.PROFILE_HEADER: (1536, 256), K.PROFILE_BACKGROUND: (1920, 1080)}

# Имя, редкость, рисовалка. Порядок тот же, в каком лягут в инвентарь.
HEADERS = [
    ("Ночь", R.COMMON, lambda s: art.mesh(s, art.NIGHT * 2 + art.VIOLET)),
    ("Глубина", R.COMMON, lambda s: art.mesh(s, art.OCEAN)),
    ("Закат", R.COMMON, lambda s: art.stripes(art.mesh(s, art.SUNSET), (255, 237, 213))),
    ("Хвоя", R.COMMON, lambda s: art.dots(art.mesh(s, art.FOREST), (187, 247, 208))),
    ("Волны", R.RARE, lambda s: art.waves(art.mesh(s, art.OCEAN), (186, 230, 253))),
    ("Лепестки", R.RARE, lambda s: art.glow(art.mesh(s, art.ROSE), [(251, 113, 133), (244, 63, 94), (253, 164, 175)], spots=9)),
    ("Сияние", R.EPIC, lambda s: art.glow(art.mesh(s, art.NIGHT), [(34, 211, 238), (16, 185, 129), (124, 58, 237)], spots=7, radius=260, alpha=120)),
    ("Звёздное поле", R.EPIC, lambda s: art.stars(art.mesh(s, art.NIGHT))),
    ("Ретровейв", R.LEGENDARY, lambda s: art.horizon(art.glow(art.mesh(s, art.VIOLET + [(190, 24, 93)]), [(236, 72, 153)], spots=4), (34, 211, 238))),
    ("Туманность", R.MYTHICAL, lambda s: art.stars(art.glow(art.mesh(s, art.NIGHT + art.VIOLET), [(124, 58, 237), (219, 39, 119), (37, 99, 235)], spots=10, radius=300, alpha=140), count=340)),
]

# У фона площадь в тридцать раз больше, поэтому пятна крупнее, а звёзд и точек — гуще.
BACKGROUNDS = [
    ("Полночь", R.COMMON, lambda s: art.mesh(s, art.NIGHT * 2 + art.VIOLET)),
    ("Бездна", R.COMMON, lambda s: art.mesh(s, art.OCEAN)),
    ("Ельник", R.COMMON, lambda s: art.dots(art.mesh(s, art.FOREST), (187, 247, 208), step=64, size=3)),
    ("Прибой", R.RARE, lambda s: art.waves(art.mesh(s, art.OCEAN), (186, 230, 253), count=9, length=420, height=90)),
    ("Заря", R.RARE, lambda s: art.stripes(art.mesh(s, art.SUNSET), (255, 237, 213), step=160, width=48)),
    ("Северное сияние", R.EPIC, lambda s: art.glow(art.mesh(s, art.NIGHT), [(34, 211, 238), (16, 185, 129), (124, 58, 237)], spots=9, radius=700, alpha=120)),
    ("Млечный путь", R.EPIC, lambda s: art.stars(art.mesh(s, art.NIGHT), count=1400)),
    ("Синтвейв", R.LEGENDARY, lambda s: art.horizon(art.glow(art.mesh(s, art.VIOLET + [(190, 24, 93)]), [(236, 72, 153)], spots=5, radius=600), (34, 211, 238), rows=14)),
    ("Глубокий космос", R.MYTHICAL, lambda s: art.stars(art.glow(art.mesh(s, art.NIGHT + art.VIOLET), [(124, 58, 237), (219, 39, 119), (37, 99, 235)], spots=12, radius=800, alpha=140), count=1800)),
]

SETS = {K.PROFILE_HEADER: HEADERS, K.PROFILE_BACKGROUND: BACKGROUNDS}


class Command(BaseCommand):
    help = "Нарисовать стартовый набор шапок или фонов профиля"

    def add_arguments(self, parser):
        parser.add_argument("--kind", choices=("header", "background", "all"), default="all")
        parser.add_argument("--apply", action="store_true", help="без него только показывает, что нарисует")
        parser.add_argument("--refresh", action="store_true", help="перерисовать уже существующие")
        parser.add_argument("--seed", type=int, default=20260820, help="зерно случайности: одно и то же даёт те же картинки")

    def handle(self, *args, **options):
        random.seed(options["seed"])
        wanted = {"header": [K.PROFILE_HEADER], "background": [K.PROFILE_BACKGROUND]}.get(
            options["kind"], list(SETS),
        )
        done = 0
        for kind in wanted:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{CosmeticItem.Kind(kind).label}:"))
            done += self.draw_set(kind, options)

        self.stdout.write(f"\nНарисовано: {done}")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Пробный прогон. Записать: --apply"))

    def draw_set(self, kind, options):
        done = 0
        for name, rarity, draw in SETS[kind]:
            item = CosmeticItem.objects.filter(source=f"generated:{name}").first()
            if item and not options["refresh"]:
                self.stdout.write(f"  уже есть: {name}")
                continue

            body = art.png(draw(SIZES[kind]))
            self.stdout.write(
                f"  {name:18} {rarity:10} {len(body) // 1024:>5} КБ"
                f" · {'перерисовано' if item else 'нарисовано'}"
            )
            if options["apply"]:
                self.store(item or CosmeticItem(
                    name=name, rarity=rarity, kind=kind, source=f"generated:{name}",
                ), name, body)
            done += 1
        return done

    def store(self, item, name, body):
        if item.pk:
            item.image.delete(save=False)
        item.image.save(f"{name}.png", ContentFile(body), save=False)
        item.save()
