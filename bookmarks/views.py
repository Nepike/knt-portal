"""Закладки: кнопка в шапке и страница со всем помеченным.

От вида вещи тут не зависит почти ничего — только одно: как найти ту вещь, которая
этому человеку ВИДНА. Иначе по прямому адресу можно было бы пометить чужой черновик
и увидеть его название в своём списке.
"""

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from .models import Bookmark

# Как показывать каждый вид на странице закладок: заголовок группы и значок.
# Значки те же, что у разделов в левом меню, — вещь и её раздел должны узнаваться.
GROUPS = (
    ("material", "Материалы", "fa-folder"),
    ("book", "Книги", "fa-book"),
    ("playlist", "Курсы лекций", "fa-video"),
    ("teacher", "Преподаватели", "fa-user-tie"),
)


def owners(kind, user):
    """Вещи этого вида, видимые человеку.

    Импорт внутри функции намеренно: приложения владельцев зовут отсюда кнопку для своей
    страницы, и на уровне модуля это был бы круг в импортах — ровно как в `comments`.
    """
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
    """Что нужно кнопке в шапке. Зовут отсюда страницы, которым есть что помечать.

    Вид спрашиваем у самой модели: поля закладки названы по ней (`material`, `book`, …),
    как и у `attachments.File`, — и второй список имён рано или поздно разошёлся бы с первым.
    """
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
    # get_or_create, а не «поискать и создать»: два быстрых нажатия иначе оба увидели бы
    # «не помечено» и второе упало бы на уникальности пятисоткой.
    bookmark, added = Bookmark.objects.get_or_create(user=request.user, **{kind: owner})
    if not added:
        bookmark.delete()
    return render(request, "bookmarks/_button.html", {"kind": kind, "item": owner, "saved": added})


def _groups(user):
    """Закладки по видам, готовыми строками.

    Собираем здесь, а не в шаблоне: поля у четырёх видов разные, и один цикл превратился
    бы в четыре ветки внутри себя. Пустые группы не возвращаем вовсе — заголовок «Книги»
    над пустотой сообщает только о том, что книг не помечено, а это и так видно.
    """
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
    """Строка списка: куда вести, что написать и чем подписать.

    Название берём полем, а не `str()`: у материала, книги и курса он начинается с номера
    («#249: Зорич») — это подпись для админки, а человеку номер не нужен. У преподавателя
    поля `title` нет вовсе, и там как раз годится `str()` — это его фамилия с инициалами.
    """
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
    """Убрать закладку со страницы закладок.

    Отдельно от `bookmark_toggle`: там ответ — кнопка в шапке, а здесь строка должна
    исчезнуть из списка. Перерисовываем список целиком — вместе со строкой уходит и её
    группа, если она была последней, и число в заголовке.
    """
    get_object_or_404(Bookmark, pk=pk, user=request.user).delete()
    groups = _groups(request.user)
    return render(request, "bookmarks/_list.html", {
        "groups": groups,
        "total": sum(len(group["rows"]) for group in groups),
    })
