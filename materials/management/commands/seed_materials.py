"""Демо-материалы, чтобы посмотреть страницы вживую. ТОЛЬКО для разработки.

Всё созданное убирается обратно: `seed_materials --wipe` находит материалы по
названиям из DEMO и удаляет их вместе с файлами (блобы снимет post_delete).
"""

import random
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from PIL import Image as PilImage, ImageDraw

from attachments.models import File, Image
from core.models import Subject, Term
from materials.models import Material
from teachers.models import Teacher
from users.models import User

TEXT = """## Что внутри

Конспект лекций за семестр, набранный по ходу занятий. Разбиты **по темам**,
формулы вынесены в рамки, в конце — разбор типовых задач.

### Темы

- предел последовательности и функции
- непрерывность, точки разрыва
- производная и её применение
- интеграл: неопределённый и определённый

> Если нашли ошибку — пишите в комментарии, поправлю.

Ключевая формула семестра — определение производной:

$$f'(x) = \\lim_{h \\to 0} \\frac{f(x+h) - f(x)}{h}$$

В тексте формулы тоже работают: если $f(x) = x^n$, то $f'(x) = n x^{n-1}$.

Полезные ссылки: [методичка кафедры](https://mipt.ru), [задачник](https://mipt.ru).

| Тема | Задач | Сложность |
|---|---|---|
| Пределы | 24 | средняя |
| Производные | 31 | лёгкая |
| Интегралы | 40 | высокая |

Код для проверки численно:

    def limit(f, x0, eps=1e-6):
        return (f(x0 + eps) - f(x0 - eps)) / 2

Остальное — в файлах.
"""

# (название, короткое описание, год, кусок названия предмета)
# Предмет ищем по подстроке, а не берём случайный: «конспект по матанализу» с предметом
# «Английский язык» выглядит как сломанная демка, а не как демка.
DEMO = [
    ("Конспект лекций по матанализу", "Полный конспект за первый семестр, с разбором задач.", 2026, "анализ"),
    ("Семинары: пределы и непрерывность", "Разобранные задачи с семинаров, 12 листков.", 2026, "анализ"),
    ("Билеты к экзамену по механике", "Ответы на все 40 билетов, проверены после экзамена.", 2026, "Физика"),
    ("Лабораторные работы по общей физике", "Шаблоны отчётов и обработанные данные измерений.", 2025, "Физика"),
    ("Коллоквиум по линейной алгебре", "Теория одним листом плюс типовые доказательства.", 2025, "алгебра"),
    ("Задачи прошлых лет по термодинамике", "Контрольные и экзамены за пять лет с решениями.", 2025, "Физика"),
    ("Конспект по теории вероятностей", "Аккуратный конспект, местами с примерами из жизни.", 2024, "анализ"),
    ("Программирование: разбор домашек", "Все домашние задания семестра с комментариями.", 2024, "Программирование"),
    ("Электричество и магнетизм: формулы", "Шпаргалка на две страницы, только формулы.", 2023, "Физика"),
]

FILES = [
    ("Лекции. Часть 1.pdf", b"%PDF-1.4 demo\n"),
    ("Лекции. Часть 2.pdf", b"%PDF-1.4 demo\n"),
    ("Разбор задач.docx", b"demo docx\n"),
    ("Таблица результатов.xlsx", b"demo xlsx\n"),
]


COLORS = ["#0ea5e9", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444"]


def demo_picture(label, color):
    """Заглушка вместо фото доски: цветной прямоугольник с подписью."""
    picture = PilImage.new("RGB", (640, 400), color)
    ImageDraw.Draw(picture).text((24, 24), label, fill="white")
    buffer = BytesIO()
    picture.save(buffer, format="JPEG", quality=70)
    return ContentFile(buffer.getvalue(), name=f"{label}.jpg")


class Command(BaseCommand):
    help = "Заливает демо-материалы (только для разработки). --wipe удаляет их обратно."

    def add_arguments(self, parser):
        parser.add_argument("--wipe", action="store_true", help="удалить ранее созданные демо-материалы")
        parser.add_argument("--files", type=int, default=2, help="сколько файлов вешать на материал (0 — без файлов)")
        parser.add_argument("--images", type=int, default=3, help="сколько картинок класть в галерею")

    def handle(self, *args, **options):
        titles = [item[0] for item in DEMO]

        if options["wipe"]:
            # Удаляем по одному: post_delete снимает блобы, а bulk-delete сигналы шлёт,
            # но файл у каждого свой — так надёжнее и видно, сколько ушло.
            gone = 0
            for material in Material.objects.filter(title__in=titles):
                material.delete()
                gone += 1
            self.stdout.write(self.style.SUCCESS(f"удалено демо-материалов: {gone}"))
            return

        subjects = list(Subject.objects.all())
        if not subjects:
            raise CommandError("Нет ни одного предмета — сначала заведи их в админке.")
        terms = list(Term.objects.all())
        teachers = list(Teacher.objects.all())
        uploader = User.objects.order_by("pk").first()

        random.seed(20260807)  # один и тот же набор при каждом прогоне
        created = 0
        for index, (title, synopsis, year, hint) in enumerate(DEMO):
            if Material.objects.filter(title=title).exists():
                continue
            subject = Subject.objects.filter(name__icontains=hint).first() or random.choice(subjects)
            with transaction.atomic():
                material = Material.objects.create(
                    title=title, synopsis=synopsis, year=year, text=TEXT,
                    subject=subject, uploader=uploader,
                    # Последние два оставляем на проверке — видно и бейдж, и очередь модерации.
                    status=Material.Status.PENDING if index >= len(DEMO) - 2 else Material.Status.APPROVED,
                )
                if terms:
                    material.terms.add(*random.sample(terms, min(len(terms), random.randint(1, 2))))
                if teachers:
                    material.teachers.add(*random.sample(teachers, min(len(teachers), random.randint(1, 2))))
                for order, (name, body) in enumerate(FILES[: options["files"]]):
                    File.objects.create(
                        material=material, name=name, order=order, uploader=uploader,
                        file=ContentFile(body, name=name),
                    )
                for order in range(options["images"]):
                    label = f"Доска {order + 1}"
                    Image.objects.create(
                        material=material, name=label, order=order, uploader=uploader,
                        image=demo_picture(label, COLORS[(index + order) % len(COLORS)]),
                    )
            created += 1

        self.stdout.write(self.style.SUCCESS(
            f"создано материалов: {created} (убрать: manage.py seed_materials --wipe)"
        ))
