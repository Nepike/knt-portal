"""Перенос картинки на Стену: подгон размера, перевод в палитру, запись. Без --apply только
показывает, что выйдет: доска общая. Клетки ложатся слоями по цветам — в таймлапсе картинка
проступает целиком, а не выезжает строками.
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from PIL import Image

from users.models import User
from wall import palette
from wall.models import Board
from wall.services import SKIP, WallError, fit, stamp

PREVIEW_ZOOM = 8  # во столько раз крупнее сохраняем примерку: клетки должны быть видны


class Command(BaseCommand):
    help = "Нарисовать картинку на Стене (без --apply только примеряет)"

    def add_arguments(self, parser):
        parser.add_argument("image", help="путь к картинке")
        parser.add_argument("--user", required=True, help="почта автора; нужны права модератора Стены")
        parser.add_argument("--x", type=int, help="левый край в клетках, по умолчанию по центру")
        parser.add_argument("--y", type=int, help="верхний край в клетках")
        parser.add_argument("--width", type=int, help="ширина на доске в клетках, по умолчанию во всю доску")
        parser.add_argument("--preview", help="куда сохранить примерку картинкой")
        parser.add_argument("--apply", action="store_true", help="без него доска не меняется")

    def handle(self, *args, **options):
        board = Board.current()
        if board is None:
            raise CommandError("открытой доски нет")
        author = User.objects.filter(email=options["user"]).first()
        if author is None:
            raise CommandError(f"нет пользователя {options['user']}")

        picture = Image.open(options["image"]).convert("RGBA")
        wide, tall = self._size(board, picture, options["width"])
        # BOX — усреднение по площади: у снятого с экрана пиксель-арта клетка — ровный квадрат одного цвета.
        picture = picture.resize((wide, tall), Image.Resampling.BOX)

        left = (board.width - wide) // 2 if options["x"] is None else options["x"]
        top = (board.height - tall) // 2 if options["y"] is None else options["y"]
        if not board.holds(left, top) or not board.holds(left + wide - 1, top + tall - 1):
            raise CommandError(f"{wide}×{tall} в ({left}, {top}) не помещается на {board.width}×{board.height}")

        cells = self._map(picture, left, top)
        counts = Counter(cells.values())
        self.stdout.write(f"{wide}×{tall} клеток в ({left}, {top}); занято {len(cells)}, цветов {len(counts)}")
        for code, count in counts.most_common(10):
            self.stdout.write(f"  {palette.get(code).name}: {count}")

        if options["preview"]:
            self._preview(cells, left, top, wide, tall, options["preview"])
            self.stdout.write(f"примерка сохранена: {options['preview']}")

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("это примерка, доска не тронута — повтори с --apply"))
            return
        try:
            placements = stamp(author, board, cells)
        except WallError as error:
            raise CommandError(str(error))
        self.stdout.write(self.style.SUCCESS(f"положено клеток: {len(placements)}"))

    def _size(self, board, picture, width):
        """Во всю доску, если ширину не задали. Пропорции держим: растянутый арт не арт."""
        wide = width or board.width
        tall = max(1, round(picture.height * wide / picture.width))
        if width is None and tall > board.height:
            tall = board.height
            wide = max(1, round(picture.width * tall / picture.height))
        return wide, tall

    def _map(self, picture, left, top):
        """Каждой клетке ближайший цвет палитры (services.fit). Прозрачное пропускаем — штамп поверх нарисованного."""
        codes = fit(picture.width, picture.height, picture.tobytes())
        return {
            (left + index % picture.width, top + index // picture.width): code
            for index, code in enumerate(codes) if code != SKIP
        }

    def _preview(self, cells, left, top, wide, tall, path):
        sheet = Image.new("RGB", (wide, tall), palette.rgb(palette.CONCRETE))
        for (x, y), code in cells.items():
            sheet.putpixel((x - left, y - top), palette.rgb(palette.get(code).hex))
        sheet.resize((wide * PREVIEW_ZOOM, tall * PREVIEW_ZOOM), Image.Resampling.NEAREST).save(path)
