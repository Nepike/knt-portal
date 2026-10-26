"""Действия Пульса над контентом: удалить отзыв или комментарий, вернуть работу на проверку.

Права — те же, что на сайте: удаление отзыва — teachers.delete_review, комментария —
comments.change_comment, проверка — право правки самой работы (как в moderation/queue.py).
Удаление — через rewards.remove: удаляет не автор, и с него ничего не списывается.
"""

from dataclasses import dataclass

from django.contrib.admin.models import CHANGE, DELETION
from django.urls import reverse

from comments.models import Comment
from core.models import Moderated
from economy import rewards
from lectorium.models import Playlist
from library.models import Book
from materials.models import Material
from teachers.models import Review

from .access import logged

# вид в адресе → (модель, право, как назвать в истории, о чём спросить)
REMOVABLE = {
    "review": (Review, "teachers.delete_review", "отзыв", "Удалить отзыв? С автора ничего не спишется — удаляет не он."),
    "comment": (
        Comment, "comments.change_comment", "комментарий",
        "Удалить комментарий вместе с ответами? С автора ничего не спишется.",
    ),
}
REVIEWABLE = {
    "material": (Material, "materials.change_material"),
    "book": (Book, "library.change_book"),
    "playlist": (Playlist, "lectorium.change_playlist"),
}
BACK_TO_REVIEW = "Вернуть на проверку? Пока работу снова не опубликуют, на сайте её не видно."


@dataclass
class Action:
    label: str
    url: str
    question: str


def _kind_of(obj, table):
    return next((kind for kind, (model, *_) in table.items() if isinstance(obj, model)), None)


def offered(events, user):
    """Приписать событиям действия, доступные этому человеку."""
    for event in events:
        obj = event.obj
        if obj is None:
            continue
        kind = _kind_of(obj, REMOVABLE)
        if kind and user.has_perm(REMOVABLE[kind][1]):
            event.actions.append(Action("Удалить", reverse("pulse_remove", args=[kind, obj.pk]), REMOVABLE[kind][3]))
        kind = _kind_of(obj, REVIEWABLE)
        if kind and obj.status == Moderated.Status.APPROVED and user.has_perm(REVIEWABLE[kind][1]):
            event.actions.append(Action("Вернуть на проверку", reverse("pulse_unpublish", args=[kind, obj.pk]), BACK_TO_REVIEW))
    return events


def remove(request, obj, kind):
    logged(request, obj, f"удалён {REMOVABLE[kind][2]}", DELETION)
    rewards.remove(obj, by=request.user)


def unpublish(request, obj):
    obj.send_to_review()
    obj.save(update_fields=Moderated.REVIEW_FIELDS)
    logged(request, obj, "возвращено на проверку", CHANGE)
