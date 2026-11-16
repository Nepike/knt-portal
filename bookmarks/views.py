"""Закладки: кнопка в шапке и страница со всем помеченным. От вида зависит одно — как найти
вещь, ВИДИМУЮ человеку: иначе можно пометить чужой черновик.
"""

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from .models import Bookmark

# Группы на странице закладок: заголовок и значок (как у раздела в меню).
GROUPS = (
    ("material", "Материалы", "fa-folder"),
    ("book", "Книги", "fa-book"),
    ("playlist", "Курсы лекций", "fa-video"),
    ("teacher", "Преподаватели", "fa-user-tie"),
)


def owners(kind, user):
    """Вещи этого вида, видимые человеку. Импорт внутри: приложения владельцев сами зовут
    отсюда кнопку, на уровне модуля вышел бы круг."""
    if kind == "material":
        from materials.views import visible

        return visible(user)
    if kind == "book":
        from library.views import visible

        return visible(user)
    if kind == "playlist":
        from lectorium.views import visible_playlists

        return visible_playlists(user)
    if kind == "teacher":
        from teachers.models import Teacher

        return Teacher.objects.all()  # преподаватели открыты все
    raise Http404


def button(user, owner):
    """Что нужно кнопке в шапке. Вид — из имени модели: так названы поля закладки."""
    kind = owner._meta.model_name
    return {
        "kind": kind,
        "item": owner,
        "saved": Bookmark.objects.filter(user=user, **{kind: owner}).exists(),
    }


@require_POST
def bookmark_toggle(request, kind, pk):
    """Пометить или снять пометку. Повторное нажатие снимает — другой кнопки для этого нет."""
    owner = get_object_or_404(owners(kind, request.user), pk=pk)
    # get_or_create: два быстрых нажатия иначе упали бы на уникальности.
    bookmark, added = Bookmark.objects.get_or_create(user=request.user, **{kind: owner})
    if not added:
        bookmark.delete()
    return render(request, "bookmarks/_button.html", {"kind": kind, "item": owner, "saved": added})


def _groups(user):
    """Закладки по видам, готовыми строками; пустых групп нет."""
    # Предметы преподавателя идут в подпись — без prefetch это запрос на каждую строку.
    saved = Bookmark.objects.filter(user=user).select_related(
        "material__subject", "book", "playlist__subject", "teacher",
    ).prefetch_related("teacher__subjects")
    by_kind = {kind: [] for kind, _, _ in GROUPS}
    for bookmark in saved:
        if bookmark.kind:
            by_kind[bookmark.kind].append(bookmark)

    groups = []
    for kind, title, icon in GROUPS:
        rows = [_row(bookmark) for bookmark in by_kind[kind]]
        if rows:
            groups.append({"title": title, "icon": icon, "rows": rows})
    return groups


def _row(bookmark):
    """Строка списка. Название — полем, а не `str()` (там номер для админки); у преподавателя
    `str()` и есть фамилия с инициалами."""
    owner = bookmark.owner
    return {
        "pk": bookmark.pk,
        "url": owner.get_absolute_url(),
        "title": getattr(owner, "title", "") or str(owner),
        "note": _note(bookmark),
    }


def _note(bookmark):
    """Подпись под названием — то немногое, чем виды и различаются."""
    if bookmark.material_id:
        return bookmark.material.subject.name
    if bookmark.book_id:
        return bookmark.book.authors
    if bookmark.playlist_id:
        return bookmark.playlist.subject.name
    return ", ".join(subject.name for subject in bookmark.teacher.subjects.all())


def bookmark_list(request):
    groups = _groups(request.user)
    return render(request, "bookmarks/bookmarks.html", {
        "groups": groups,
        "total": sum(len(group["rows"]) for group in groups),
    })


@require_POST
def bookmark_drop(request, pk):
    """Убрать закладку со страницы закладок: перерисовываем список целиком (группа, число)."""
    get_object_or_404(Bookmark, pk=pk, user=request.user).delete()
    groups = _groups(request.user)
    return render(request, "bookmarks/_list.html", {
        "groups": groups,
        "total": sum(len(group["rows"]) for group in groups),
    })
