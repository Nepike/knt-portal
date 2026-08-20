"""Перенос рамок аватара: со старого сайта и из папки с неразобранными.

    manage.py import_frames --db D:\\knt-legacy\\db.sqlite3 --media D:\\knt-legacy\\media
    manage.py import_frames --more "C:\\...\\more frames" --apply

У старых рамок есть имя и редкость — берём из базы. У новых нет ничего, имена файлов
это хеши, поэтому им ставится «Рамка N» и «обычная»: переименовать удобнее в админке,
где видно саму картинку.

Сама анимация кладётся КАК ЕСТЬ, в исходном формате. Рядом сохраняется обложка —
один кадр, для админки и будущих витрин. Кадр берётся НЕ первый: у половины рамок
анимация начинается с пустоты, и обложкой оказался бы прозрачный квадрат.
"""

import sqlite3
from io import BytesIO
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from PIL import Image, ImageSequence

from cosmetics.models import CosmeticItem

# Столько же, сколько у исходников со старого сайта и у стимовских: рамка квадратная,
# аватар утапливается внутрь на 10% (см. core/_avatar.html).
# Сторона обложки. Сама анимация не трогается вовсе, так что размер тут только про неё.
SIDE = 224


class Command(BaseCommand):
    help = "Перенести рамки аватара: из базы старого сайта и/или из папки с файлами"

    def add_arguments(self, parser):
        parser.add_argument("--db", help="db.sqlite3 старого сайта")
        parser.add_argument("--media", help="каталог media старого сайта (рядом с --db)")
        parser.add_argument("--more", help="папка с рамками без имён")
        parser.add_argument("--refresh", action="store_true", help="перезаписать файлы у уже перенесённых")
        parser.add_argument("--apply", action="store_true", help="без него только показывает, что перенёс бы")

    def handle(self, *args, **options):
        self.apply = options["apply"]
        self.refresh = options["refresh"]
        if not options["db"] and not options["more"]:
            raise CommandError("нечего переносить: укажи --db или --more")

        done = 0
        if options["db"]:
            if not options["media"]:
                raise CommandError("с --db нужен и --media: в базе лежат пути, а не файлы")
            done += self.from_legacy(Path(options["db"]), Path(options["media"]))
        if options["more"]:
            done += self.from_folder(Path(options["more"]))

        self.stdout.write(f"\nПеренесено: {done}")
        if not self.apply:
            self.stdout.write(self.style.WARNING("Пробный прогон. Записать: --apply"))

    def from_legacy(self, db, media):
        """Старый сайт: имя и редкость известны, поэтому переносим как есть."""
        if not db.exists():
            raise CommandError(f"нет файла базы: {db}")
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select id, name, rarity, image from activity_cosmeticitem where item_type = 'avatar_frame'"
        ).fetchall()

        done = 0
        for row in rows:
            source = media / row["image"]
            if not source.exists():
                self.stdout.write(self.style.WARNING(f"  нет файла: {row['image']}"))
                continue
            done += self.take(source, row["name"], row["rarity"], f"legacy:{row['id']}")
        return done

    def from_folder(self, folder):
        """Папка с хешами вместо имён: редкость и название расставит человек в админке."""
        if not folder.is_dir():
            raise CommandError(f"нет такой папки: {folder}")

        taken = set(CosmeticItem.objects.values_list("name", flat=True))
        number = 1
        done = 0
        for source in sorted(folder.iterdir()):
            if source.suffix.lower() not in (".png", ".apng", ".webp", ".gif"):
                continue
            # Имя придумываем только тому, кого ещё не переносили: иначе на втором
            # заходе те же файлы получили бы новые номера и легли повторно. Уже
            # известной вещи возвращаем её собственное — оно могло быть исправлено руками.
            origin = f"file:{source.name}"
            known = CosmeticItem.objects.filter(source=origin).values_list("name", flat=True).first()
            if known:
                name = known
            else:
                while (name := f"Рамка {number}") in taken:
                    number += 1
                taken.add(name)
            done += self.take(source, name, CosmeticItem.Rarity.COMMON, origin)
        return done

    def take(self, source, name, rarity, origin):
        item = CosmeticItem.objects.filter(source=origin).first()
        if item and not self.refresh:
            self.stdout.write(f"  уже есть: {name}")
            return 0
        if item is None and CosmeticItem.objects.filter(name=name).exists():
            self.stdout.write(self.style.WARNING(f"  имя занято, пропускаю: {name}"))
            return 0
        try:
            animated, still, frames = self.convert(source)
        except OSError as error:
            self.stdout.write(self.style.ERROR(f"  не открылась {source.name}: {error}"))
            return 0

        mark = "обновлено" if item else "перенесено"
        self.stdout.write(
            f"  {name:22} {rarity:10} {frames:>4} кадр. "
            f"{len(animated) // 1024:>5} КБ + {len(still) // 1024} КБ обложка · {mark}"
        )
        if self.apply:
            self.store(item or CosmeticItem(name=name, rarity=rarity, source=origin), source, animated, still)
        return 1

    def store(self, item, source, animated, still):
        """Записать файлы. У обновляемой вещи прежние блобы снимаем сами: подменённое
        поле про них уже не помнит, и в хранилище остались бы сироты."""
        if item.pk:
            item.image.delete(save=False)
            item.still.delete(save=False)
        item.image.save(f"{source.stem}{source.suffix.lower()}", ContentFile(animated), save=False)
        item.still.save(f"{source.stem}-still.png", ContentFile(still), save=False)
        item.save()

    def convert(self, source):
        """Анимация как есть + обложка одним кадром.

        Раньше здесь APNG пережимался в анимированный WebP: на всей коллекции это
        экономило 40% (34.1 → 20.6 МБ). Отказались: WebP-анимация лоссИ, на пиксельных
        рамках потери видны глазом, а показываем мы за раз одну рамку в профиле и горстку
        в инвентаре. Тринадцать мегабайт в бакете таких потерь не стоят.
        """
        with Image.open(source) as image:
            frames = [_square(frame.convert("RGBA")) for frame in ImageSequence.Iterator(image)]

        return source.read_bytes(), _bytes(_cover(frames), "PNG", optimize=True), len(frames)


def _square(frame):
    """Обложку приводим к общей стороне. Почти все рамки и так 224×224 — тогда не трогаем
    вовсе, чтобы не размывать пиксельные интерполяцией на ровном месте."""
    return frame if frame.size == (SIDE, SIDE) else frame.resize((SIDE, SIDE), Image.LANCZOS)


def _cover(frames):
    """Кадр для обложки — самый видимый.

    Первый брать нельзя: у половины рамок анимация начинается с пустоты, и обложкой
    оказался бы прозрачный квадрат. Считаем СУММУ непрозрачности, а не число заметных
    точек: у мягких свечений («PULSE [RED]») альфа везде низкая, и по порогу такой
    кадр не отличался бы от пустого.
    """
    return max(frames, key=lambda frame: sum(frame.getchannel("A").get_flattened_data()))


def _bytes(image, fmt, **options):
    buffer = BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()
