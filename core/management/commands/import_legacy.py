"""Перенос данных со старого сайта (knt-website) в новый.

Старая база — SQLite; читаем её напрямую модулем sqlite3. Поднимать ради этого старый
Django незачем: модели там всё равно другие, а так у переноса нет ни одной зависимости
от чужого кода.

**Id сохраняются.** У пользователя, преподавателя, предмета, семестра, группы, материала,
книги, файла и картинки номер остаётся прежним. Отсюда два следствия: старые ссылки вида
/materials/123/ продолжают работать, а файл, который сейчас не скачать (лежит на упавшем
local.inbicst.ru), потом находится в старой базе по тому же id — отдельный список
«что откуда качать» не нужен, старая база сама себе манифест.

Не переносим намеренно: роли и права (выдаются заново), баланс и магазин, косметику,
обратную связь, сессии, комментарии к материалам.

    manage.py import_legacy --db D:/knt-legacy/db.sqlite3 --media D:/knt-legacy/media
    manage.py import_legacy --db ... --media ... --apply
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from django.core.files import File as DjangoFile
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction

from attachments.models import File, Image
from attachments.storage import random_key
from core.models import Subject, Team, Term
from library.models import Book
from materials.models import Material
from teachers.models import Review, Teacher
from users.models import User

SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGT]?B)$")
MAX_SCORE = 5


def to_bytes(human):
    """«12.34 MB» → байты. Старая база хранила размер строкой для показа."""
    match = SIZE_RE.match((human or "").strip())
    if not match:
        return None
    return int(float(match.group(1)) * SIZE_UNITS[match.group(2)])


def text(value):
    """Старые поля сплошь null=True там, где новые просто пустые."""
    return (value or "").strip()


def score(value):
    """0 в старой базе значило «не оценил» — в новой это None, а не ноль."""
    if not value:
        return None
    return min(int(value), MAX_SCORE)


def when(value):
    """Дата-время из SQLite. Старый сайт тоже жил с USE_TZ, значит там лежит UTC
    без пометки о зоне — её и возвращаем на место, иначе время съедет на три часа."""
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class Command(BaseCommand):
    help = "Переносит пользователей, преподавателей, материалы и книги со старого сайта."

    def add_arguments(self, parser):
        parser.add_argument("--db", required=True, help="db.sqlite3 старого сайта")
        parser.add_argument("--media", required=True, help="каталог media старого сайта")
        parser.add_argument("--apply", action="store_true", help="без него всё откатывается")

    def handle(self, *args, **options):
        self.old_db = Path(options["db"])
        self.old_media = Path(options["media"])
        self.apply = options["apply"]
        if not self.old_db.is_file():
            raise CommandError(f"нет файла базы: {self.old_db}")
        if not self.old_media.is_dir():
            raise CommandError(f"нет каталога медиа: {self.old_media}")

        occupied = [m.__name__ for m in (User, Teacher, Material, Book, File, Image) if m.objects.exists()]
        if occupied:
            raise CommandError(
                f"в базе уже есть данные ({', '.join(occupied)}). Перенос идёт на пустую базу — "
                "иначе номера столкнутся с существующими."
            )

        self.old = sqlite3.connect(f"file:{self.old_db}?mode=ro", uri=True)
        self.old.row_factory = sqlite3.Row
        self.counts = {}
        self.notes = []

        # Пробный прогон — та же работа, но с откатом: так он проверяет и ограничения БД,
        # а не только наши догадки о данных.
        try:
            with transaction.atomic():
                self.run()
                if not self.apply:
                    transaction.set_rollback(True)
        finally:
            self.old.close()

        self.report()

    def run(self):
        self.teams()
        self.subjects()
        self.terms()
        self.users()
        self.teachers()
        self.reviews()
        self.books()
        self.materials()
        self.files()
        self.images()
        self.photos()
        if self.apply:
            self.fix_sequences()

    def rows(self, table, where=""):
        return self.old.execute(f"select * from {table} {where}")  # noqa: S608 — имена таблиц наши

    def done(self, what, n):
        self.counts[what] = n
        self.stdout.write(f"  {what}: {n}")

    def note(self, message):
        self.notes.append(message)

    def clip(self, value, limit, what):
        value = text(value)
        if len(value) > limit:
            self.note(f"обрезано до {limit}: {what} — «{value[:60]}…»")
            return value[:limit]
        return value

    # ── справочники ───────────────────────────────────────────────────────────────

    def teams(self):
        Team.objects.bulk_create(
            Team(
                pk=row["id"], number=row["number"], profile=text(row["profile"]),
                course_code=text(row["course_code"]), stage=row["stage"],
                year_of_admission=int(row["year_of_admission"]),
            )
            for row in self.rows("core_team")
        )
        self.done("учебные группы", Team.objects.count())

    def subjects(self):
        Subject.objects.bulk_create(
            Subject(
                pk=row["id"], name=text(row["name"]),
                dative=text(row["dative"]), accusative=text(row["accusative"]),
            )
            for row in self.rows("core_subject")
        )
        self.done("предметы", Subject.objects.count())

    def terms(self):
        Term.objects.bulk_create(Term(pk=row["id"], number=row["number"]) for row in self.rows("core_term"))
        self.done("семестры", Term.objects.count())

    # ── люди ──────────────────────────────────────────────────────────────────────

    def users(self):
        User.objects.bulk_create(
            User(
                pk=row["id"],
                # Хеш переносим как есть: алгоритм тот же (pbkdf2_sha256), пароли остаются рабочими.
                password=row["password"], last_login=when(row["last_login"]),
                email=row["email"], name=text(row["name"]), surname=text(row["surname"]),
                patronymic=text(row["patronymic"]),
                is_active=bool(row["is_active"]),
                # Роли выдаются заново вручную — старые группы и флаги не переносим.
                is_staff=False, is_superuser=False,
                must_change_password=bool(row["must_change_password"]),
                birthday=row["birthday"], date_joined=when(row["date_joined"]),
                phone=text(row["phone"]), vk_page=text(row["vk_page"]), tg_page=text(row["tg_page"]),
                mailing_allowed=bool(row["mailing_allowed"]), note=text(row["note"]),
                team_id=row["team_id"],
            )
            for row in self.rows("core_user")
        )
        self.done("пользователи", User.objects.count())

    def teachers(self):
        Teacher.objects.bulk_create(
            Teacher(
                pk=row["id"], name=text(row["name"]), surname=text(row["surname"]),
                patronymic=text(row["patronymic"]), bio=text(row["bio"]), birthday=row["birthday"],
                phone=text(row["phone"]), email=text(row["email"]),
                vk_page=text(row["vk_page"]), tg_page=text(row["tg_page"]),
            )
            for row in self.rows("teachers_teacher")
        )
        self.link("teachers_teacher_subjects", Teacher.subjects.through, "teacher_id", "subject_id")
        self.done("преподаватели", Teacher.objects.count())

    def reviews(self):
        # Оценки и рейтинги преподавателя новый сайт считает запросом (with_ratings),
        # поэтому денормализованные score_*_val/cnt старой таблицы переносить нечего.
        skipped = 0
        batch = []
        for row in self.rows("teachers_review"):
            scores = {f: score(row[f]) for f in
                      ("score_knowledge", "score_skill", "score_communication", "score_freeloading")}
            body = text(row["text"])
            if not body and not any(scores.values()):
                skipped += 1  # ни оценки, ни слова — переносить нечего
                continue
            batch.append(Review(
                pk=row["id"], teacher_id=row["teacher_id"], author_id=row["author_id"],
                hide_author=bool(row["hide_author"]), text=body, created=when(row["date"]), **scores,
            ))
        Review.objects.bulk_create(batch)

        kept = set(Review.objects.values_list("pk", flat=True))
        self.link("teachers_review_liked_users", Review.liked_users.through, "review_id", "user_id", kept)
        self.link("teachers_review_disliked_users", Review.disliked_users.through, "review_id", "user_id", kept)
        if skipped:
            self.note(f"пропущено пустых отзывов (ни оценок, ни текста): {skipped}")
        self.done("отзывы", Review.objects.count())

    # ── содержимое ────────────────────────────────────────────────────────────────

    def books(self):
        Book.objects.bulk_create(
            Book(
                pk=row["id"],
                title=self.clip(row["title"], 100, f"книга #{row['id']}"),
                authors=self.clip(row["authors"], 150, f"авторы книги #{row['id']}"),
                year=int(row["year"]) if row["year"] else None,
                uploader_id=row["uploader_id"], hide_uploader=bool(row["hide_uploader"]),
                created=when(row["date"]), **self.moderation(row),
            )
            for row in self.rows("library_book")
        )
        self.link("library_book_subjects", Book.subjects.through, "book_id", "subject_id")
        self.link("library_book_terms", Book.terms.through, "book_id", "term_id")
        self.done("книги", Book.objects.count())

    def materials(self):
        # text не переносим здесь: там Quill Delta и старый HTML, им нужен отдельный
        # проход с конвертацией в markdown (см. convert_legacy_text).
        Material.objects.bulk_create(
            Material(
                pk=row["id"],
                title=self.clip(row["title"], 100, f"материал #{row['id']}"),
                synopsis=text(row["synopsis"]), text="",
                subject_id=row["subject_id"], uploader_id=row["uploader_id"],
                hide_uploader=bool(row["hide_uploader"]),
                year=int(row["year"]) if row["year"] else when(row["date"]).year,
                created=when(row["date"]), **self.moderation(row),
            )
            for row in self.rows("materials_material")
        )
        self.link("materials_material_teachers", Material.teachers.through, "material_id", "teacher_id")
        self.link("materials_material_terms", Material.terms.through, "material_id", "term_id")
        self.done("материалы", Material.objects.count())

    def moderation(self, row):
        """Флаг «одобрен» → статус. Кто и когда проверил, старая база не хранила."""
        status = Material.Status.APPROVED if row["approved"] else Material.Status.PENDING
        return {"status": status}

    def link(self, table, through, left, right, allowed=None):
        """M2M переносим напрямую в промежуточную таблицу: 30 тысяч add() шли бы минутами."""
        field_left, field_right = (f.name for f in through._meta.fields if f.name != "id")
        made = through.objects.bulk_create(
            through(**{f"{field_left}_id": row[left], f"{field_right}_id": row[right]})
            for row in self.rows(table)
            if allowed is None or row[left] in allowed
        )
        self.stdout.write(f"    связей {table}: {len(made)}")

    # ── блобы ─────────────────────────────────────────────────────────────────────

    def files(self):
        rows = list(self.rows("system_file"))
        orphans = [r for r in rows if not r["material_id"] and not r["book_id"]]
        if orphans:
            self.note(f"пропущено файлов без материала и книги: {len(orphans)}")

        pending = 0
        for row in rows:
            if row in orphans:
                continue
            folder = "materials" if row["material_id"] else "books"
            local = text(row["local_file"])
            key, size = "", to_bytes(row["size"])
            if local:
                key, size = self.store(local, folder, row["name"])
            else:
                pending += 1  # блоб на упавшем local.inbicst.ru, догоним отдельным проходом

            File.objects.create(
                pk=row["id"], material_id=row["material_id"], book_id=row["book_id"],
                name=text(row["name"])[:150], file=key, size=size,
                downloads=row["downloads"] or 0, order=row["order"] or 0,
                uploader_id=row["uploader_id"],
            )
        if pending:
            self.note(f"файлов без блоба (ждут local.inbicst.ru): {pending}")
        self.done("файлы", File.objects.count())

    def images(self):
        rows = [r for r in self.rows("system_image") if r["material_id"]]
        for row in rows:
            key, size = self.store(text(row["local_file"]), "images", row["name"])
            Image.objects.create(
                pk=row["id"], material_id=row["material_id"],
                name=text(row["name"])[:150], image=key, size=size,
                order=row["order"] or 0, uploader_id=row["uploader_id"],
            )
        self.done("картинки", Image.objects.count())

    def photos(self):
        for model, folder in ((User, "avatars"), (Teacher, "teachers")):
            table = "core_user" if model is User else "teachers_teacher"
            done = 0
            for row in self.rows(table, "where coalesce(photo,'') <> ''"):
                key, _ = self.store(row["photo"], folder, Path(row["photo"]).stem)
                if key:
                    model.objects.filter(pk=row["id"]).update(photo=key)
                    done += 1
            self.done(f"фото ({folder})", done)

    def store(self, relative, folder, name):
        """Кладёт блоб в активное хранилище под новым непредсказуемым ключом.

        Имя внутри ключа собираем из названия записи и настоящего расширения: в старых
        путях расширение задвоено (…Матан.pdf.pdf), а порядковый номер приклеен спереди.
        """
        if not relative:
            return "", None
        source = self.old_media / relative
        if not source.is_file():
            self.note(f"нет блоба на диске: {relative}")
            return "", None

        size = source.stat().st_size
        filename = f"{text(name) or source.stem}{source.suffix.lower()}"
        key = random_key(folder, filename)
        if not self.apply:
            # Ключ настоящий (проверяет и длину поля), а блоб не пишем: транзакция
            # откатит записи, а файлы в хранилище остались бы мусором.
            return key, size

        storage = File._meta.get_field("file").storage
        with source.open("rb") as blob:
            key = storage.save(key, DjangoFile(blob))
        return key, size

    # ── завершение ────────────────────────────────────────────────────────────────

    def fix_sequences(self):
        """Мы вставляли записи с явными id, а счётчики Postgres об этом не знают —
        без сброса первая же новая запись столкнулась бы с занятым номером."""
        models = [Team, Subject, Term, User, Teacher, Review, Book, Material, File, Image]
        statements = connection.ops.sequence_reset_sql(no_style(), models)
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    def report(self):
        for note in self.notes:
            self.stderr.write(note)
        if self.apply:
            self.stdout.write(self.style.SUCCESS("перенесено"))
        else:
            self.stdout.write(self.style.WARNING("пробный прогон, всё откачено — запусти с --apply"))
