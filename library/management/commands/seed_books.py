"""Демо-книги, чтобы посмотреть библиотеку вживую. ТОЛЬКО для разработки.

Всё созданное убирается обратно: `seed_books --wipe` находит книги по названиям
из DEMO и удаляет их вместе с файлами (блобы снимет post_delete).
"""

from io import BytesIO

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from PIL import Image as PilImage, ImageDraw

from attachments.models import File
from core.models import Subject, Term
from library.models import Book
from users.models import User

# (название, авторы, год, кусок названия предмета, семестры, файлы)
DEMO = [
    ("Математический анализ. Том 1", "Зорич В. А.", 2019, "анализ", [1, 2], ["Том 1.pdf"]),
    ("Математический анализ. Том 2", "Зорич В. А.", 2019, "анализ", [3, 4], ["Том 2.pdf"]),
    ("Курс дифференциального и интегрального исчисления", "Фихтенгольц Г. М.", 2003, "анализ", [1, 2],
     ["Том 1.pdf", "Том 2.pdf"]),
    ("Сборник задач по математическому анализу", "Кудрявцев Л. Д.", 2003, "анализ", [1, 2],
     ["Задачник.pdf", "Ответы и решения.pdf"]),
    ("Линейная алгебра и геометрия", "Кострикин А. И., Манин Ю. И.", 2005, "алгебра", [1], ["Учебник.pdf"]),
    ("Сборник задач по алгебре", "Кострикин А. И.", 2001, "алгебра", [1, 2], ["Задачник.pdf"]),
    ("Механика", "Иродов И. Е.", 2014, "Физика", [1], ["Учебник.pdf"]),
    ("Задачи по общей физике", "Иродов И. Е.", 2020, "Физика", [1, 2, 3], ["Задачник.pdf", "Решения.pdf"]),
    ("Общий курс физики. Термодинамика", "Сивухин Д. В.", 2006, "Физика", [2], ["Том 2.pdf"]),
    ("Структура и интерпретация компьютерных программ", "Абельсон Х., Сассман Дж.", 2006,
     "Программирование", [3], ["SICP.pdf"]),
    ("Алгоритмы: построение и анализ", "Кормен Т. и др.", 2013, "Программирование", [3, 4],
     ["Учебник.pdf", "Слайды лекций.pdf"]),
    ("English Grammar in Use", "Murphy R.", 2019, "Английский", [1, 2], ["Учебник.pdf", "Answers.pdf"]),
]

COLORS = ["#0ea5e9", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444", "#6366f1"]


def demo_pdf(label, color):
    """Настоящий PDF, а не подделка из байтов: он должен открываться, иначе смотреть нечего.
    Текст латиницей — со шрифтом по умолчанию Pillow кириллицу не нарисует."""
    page = PilImage.new("RGB", (595, 842), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle((0, 0, 595, 180), fill=color)
    draw.text((40, 80), f"KNT demo: {label}", fill="white")
    draw.text((40, 260), "This is a placeholder file for local development.", fill="black")
    buffer = BytesIO()
    page.save(buffer, format="PDF")
    return ContentFile(buffer.getvalue(), name="demo.pdf")


class Command(BaseCommand):
    help = "Заливает демо-книги (только для разработки). --wipe удаляет их обратно."

    def add_arguments(self, parser):
        parser.add_argument("--wipe", action="store_true", help="удалить ранее созданные демо-книги")

    def handle(self, *args, **options):
        titles = [item[0] for item in DEMO]

        if options["wipe"]:
            gone = 0
            for book in Book.objects.filter(title__in=titles):
                book.delete()  # файлы каскадом, блобы снимет post_delete
                gone += 1
            self.stdout.write(self.style.SUCCESS(f"удалено демо-книг: {gone}"))
            return

        if not Subject.objects.exists():
            raise CommandError("Нет ни одного предмета — сначала заведи их в админке.")
        uploader = User.objects.order_by("pk").first()

        created = 0
        for index, (title, authors, year, hint, terms, files) in enumerate(DEMO):
            if Book.objects.filter(title=title).exists():
                continue
            subject = Subject.objects.filter(name__icontains=hint).first()
            with transaction.atomic():
                book = Book.objects.create(
                    title=title, authors=authors, year=year, uploader=uploader,
                    # Две последние оставляем на проверке — видно и бейдж, и очередь модерации.
                    status=Book.Status.PENDING if index >= len(DEMO) - 2 else Book.Status.APPROVED,
                )
                if subject:
                    book.subjects.add(subject)
                book.terms.add(*Term.objects.filter(number__in=terms))
                for order, name in enumerate(files):
                    File.objects.create(
                        book=book, name=name, order=order, uploader=uploader,
                        file=demo_pdf(f"{index + 1}.{order + 1}", COLORS[index % len(COLORS)]),
                    )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"создано книг: {created} (убрать: manage.py seed_books --wipe)"
        ))
