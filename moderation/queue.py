"""Всё, что ждёт проверки, — одна очередь на сайт. Новый вид контента — строка в GROUPS."""

from django.db.models import Count

from lectorium.models import Playlist
from library.models import Book
from materials.models import Material

# (заголовок, модель, право, шаблон карточки, чьё число стоит на карточке, что ещё она показывает)
GROUPS = [
    ("Книги", Book, "library.change_book", "moderation/_book.html", "files", ()),
    ("Материалы", Material, "materials.change_material", "moderation/_material.html", "files", ("subject",)),
    # Проверяется курс целиком: метаданные — на плейлисте.
    ("Лекции", Playlist, "lectorium.change_playlist", "moderation/_playlist.html", "lectures", ("subject",)),
]


def allowed(user):
    """Группы, которые этому человеку вообще положено видеть."""
    return [group for group in GROUPS if user.has_perm(group[2])]


def pending(user):
    """Ожидающее проверки, по группам. Старое сверху: очередь, а не лента."""
    groups = []
    for title, model, _perm, template, counted, shown in allowed(user):
        # Число файлов и предмет — тем же запросом: иначе запрос на каждую карточку.
        items = (
            model.objects.filter(status=model.Status.PENDING)
            .select_related("uploader", *shown).annotate(parts=Count(counted)).order_by("created")
        )
        if items:
            groups.append({"title": title, "template": template, "items": items})
    return groups


def pending_count(user):
    return sum(group[1].objects.filter(status=group[1].Status.PENDING).count() for group in allowed(user))
