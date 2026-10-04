"""Рамки аватара из папки с файлами.

    manage.py import_frames --more "C:\\...\\more frames" --apply

Имён и редкости у файлов нет — названия у них хеши, — поэтому вещам ставится «Рамка N»
и «обычная»: переименовать удобнее в админке, где видно саму картинку.

Файл кладётся КАК ЕСТЬ, в исходном формате: пережатие в анимированный WebP экономит
38%, но оно лоссИ, и на пиксельных рамках это видно.
"""

from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from PIL import Image, ImageSequence

from cosmetics.models import CosmeticItem


class Command(BaseCommand):
    help = "Завести рамки аватара из папки с файлами"

    def add_arguments(self, parser):
        parser.add_argument("--more", required=True, help="папка с рамками без имён")
        parser.add_argument("--refresh", action="store_true", help="перезаписать файлы у уже перенесённых")
        parser.add_argument("--apply", action="store_true", help="без него только показывает, что перенёс бы")

    def handle(self, *args, **options):
        self.apply = options["apply"]
        self.refresh = options["refresh"]
        done = self.from_folder(Path(options["more"]))

        self.stdout.write(f"\nПеренесено: {done}")
        if not self.apply:
            self.stdout.write(self.style.WARNING("Пробный прогон. Записать: --apply"))

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
            animated, frames = self.convert(source)
        except OSError as error:
            self.stdout.write(self.style.ERROR(f"  не открылась {source.name}: {error}"))
            return 0

        mark = "обновлено" if item else "перенесено"
        self.stdout.write(f"  {name:22} {rarity:10} {frames:>4} кадр. {len(animated) // 1024:>5} КБ · {mark}")
        if self.apply:
            self.store(item or CosmeticItem(name=name, rarity=rarity, source=origin), source, animated)
        return 1

    def store(self, item, source, animated):
        """Записать файл. У обновляемой вещи прежний блоб снимаем сами: подменённое
        поле про него уже не помнит, и в хранилище остался бы сирота."""
        if item.pk:
            item.image.delete(save=False)
        item.image.save(f"{source.stem}{source.suffix.lower()}", ContentFile(animated), save=False)
        item.save()

    def convert(self, source):
        """Анимация уезжает КАК ЕСТЬ, в исходном формате. Пережатие пробовали дважды:
        анимированный WebP экономит 38%, но он лоссИ и на пиксельных рамках это видно."""
        with Image.open(source) as image:
            frames = sum(1 for _ in ImageSequence.Iterator(image))
        return source.read_bytes(), frames
