"""Что именно ждёт проверки.

Одна точка входа на весь сайт: модератор не должен обходить разделы по очереди.
Новый вид контента добавляется строкой в GROUPS — материалы и лекторий придут сюда же.
"""

from lectorium.models import Playlist
from library.models import Book
from materials.models import Material

# (заголовок, модель, право, шаблон карточки)
GROUPS = [
    ("Книги", Book, "library.change_book", "moderation/_book.html"),
    ("Материалы", Material, "materials.change_material", "moderation/_material.html"),
    # Проверяется плейлист целиком, а не отдельная лекция: метаданные на плейлисте,
    # и смотреть курс по одной записи модератору незачем.
    ("Лекции", Playlist, "lectorium.change_playlist", "moderation/_playlist.html"),
]


def allowed(user):
    """Группы, которые этому человеку вообще положено видеть."""
    return [group for group in GROUPS if user.has_perm(group[2])]


def pending(user):
    """Ожидающее проверки, по группам. Старое сверху: очередь, а не лента."""
    groups = []
    for title, model, _perm, template in allowed(user):
        items = (
            model.objects.filter(status=model.Status.PENDING)
            .select_related("uploader").order_by("created")
        )
        if items:
            groups.append({"title": title, "template": template, "items": items})
    return groups


def pending_count(user):
    return sum(
        model.objects.filter(status=model.Status.PENDING).count()
        for _title, model, _perm, _template in allowed(user)
    )
