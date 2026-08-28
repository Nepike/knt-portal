from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from attachments.models import File, human_size
from bookmarks.views import button as bookmark_button
from attachments.uploads import (
    check_pending, check_uploads, max_upload_size, pending_uploads, saved_files, sync_files, upload_limits,
)
from economy import rewards
from telegram.notify import MODERATION, notify

from .forms import BookFilterForm, BookForm
from .models import Book

PAGE_SIZE = 20  # книг в порции
# id последним — иначе на границе порций книги с одинаковым ключом перескакивают.
SORTS = {
    "popular": ("-downloads", "-id"),
    "new": ("-created", "-id"),
    "title": ("title", "-id"),
}
SORT_LABELS = {"popular": "Популярные", "new": "Новые", "title": "По названию"}


def visible(user):
    """Неопубликованную книгу видят только принёсший её и модерация."""
    if _may_moderate(user):
        return Book.objects.all()
    return Book.objects.filter(Q(status=Book.Status.APPROVED) | Q(uploader=user))


def _may_moderate(user):
    return user.has_perm("library.change_book")


def _may_edit(user, book):
    return book.uploader_id == user.pk or _may_moderate(user)


def _query(params):
    """Выбранный подбор строкой запроса.

    Пустые параметры выбрасываем, иначе в адресе висело бы «?q=&subject=&term=&sort=popular».
    `page` тоже: он про порцию, а не про подбор, — вернувшись со страницы книги, человек
    ждёт список, а не третью его страницу.
    """
    return urlencode({key: value for key, value in params.items() if value and key != "page"})


def _filters_url(request):
    """Адрес списка с текущими фильтрами: такую ссылку можно переслать, а F5 не сбросит поиск."""
    query = _query(request.GET)
    return f"{request.path}?{query}" if query else request.path


def _list_url(params):
    """Адрес списка с выбранным подбором — со страницы книги есть куда вернуться."""
    query = _query(params)
    return f"{reverse('book_list')}?{query}" if query else reverse("book_list")


def book_list(request):
    form = BookFilterForm(request.GET or None)
    q = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "")
    if sort not in SORTS:
        sort = "popular"

    # Скачивания подзапросом, а не annotate(Sum): аннотация считает по джойну, и как
    # только фильтр по предметам/семестрам начнёт матчить больше одной строки на книгу
    # (мультивыбор), сумма молча удвоится. Подзапрос от джойнов не зависит.
    totals = File.objects.filter(book=OuterRef("pk")).values("book").annotate(n=Sum("downloads")).values("n")
    books = (
        visible(request.user)
        .annotate(downloads=Coalesce(Subquery(totals), 0))
        .prefetch_related("subjects", "terms", "files")
    )
    if q:
        books = books.filter(Q(title__icontains=q) | Q(authors__icontains=q))
    if form.is_valid():
        if subject := form.cleaned_data["subject"]:
            books = books.filter(subjects=subject)
        if term := form.cleaned_data["term"]:
            books = books.filter(terms=term)

    page = Paginator(books.distinct().order_by(*SORTS[sort]), PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page": page, "books": page.object_list, "q": q, "form": form,
        "sort": sort, "sorts": SORT_LABELS.items(),
        # Подбор едет в ссылку каждой карточки — со страницы книги есть куда вернуться.
        "filters": _query(request.GET),
    }
    if not request.headers.get("HX-Request"):
        return render(request, "library/books.html", context)

    response = render(request, "library/_book_list.html", context)
    # Адрес меняем сами заголовком, а не hx-push-url на форме: так в строку уходят
    # только непустые фильтры. Догрузка порций адрес не трогает — каждая порция
    # завела бы свою запись в истории, и «назад» пришлось бы жать по разу на порцию.
    if not request.GET.get("page"):
        response["HX-Push-Url"] = _filters_url(request)
    return response


def book_detail(request, pk):
    book = get_object_or_404(
        visible(request.user).select_related("uploader").prefetch_related("subjects", "terms", "files"), pk=pk
    )
    return render(request, "library/book_detail.html", {
        "book": book,
        # Ссылка «Библиотека» ведёт не в начало списка, а туда, откуда пришли: подбор
        # приезжает сюда в адресе карточки. Иначе поиск и фильтры слетали бы на каждой
        # открытой книге — как было до этого.
        "back_url": _list_url(request.GET),
        # Кнопка закладки в шапке: она одна на весь сайт, а что помечать — знает страница.
        "bookmark": bookmark_button(request.user, book),
        "may_edit": _may_edit(request.user, book),
        "may_moderate": _may_moderate(request.user),
    })


def _files_left(request, book):
    """Сколько файлов останется у книги после сохранения: непомеченные старые плюс новые."""
    kept = sum(1 for f in book.files.all() if not request.POST.get(f"delete-{f.pk}")) if book else 0
    return kept + len(request.FILES.getlist("files")) + len(pending_uploads(request))


def book_edit(request, pk=None):
    """Одна вьюха на создание и правку: поля и работа с файлами одинаковые."""
    book = get_object_or_404(visible(request.user), pk=pk) if pk else None
    if book and not _may_edit(request.user, book):
        return HttpResponseForbidden("Эту книгу может редактировать только тот, кто её добавил.")

    form = BookForm(request.POST or None, instance=book)
    file_errors = []
    if request.method == "POST":
        file_errors = check_uploads(request.FILES.getlist("files")) + check_pending(request)
        # Книга — это и есть файл. Без него она бесполезна, в отличие от материала,
        # где есть текст и картинки.
        if not _files_left(request, book):
            file_errors.append("Добавь хотя бы один файл — без него книги не будет.")
        if form.is_valid() and not file_errors:
            book = form.save(commit=False)
            moderator = _may_moderate(request.user)
            if not book.pk:
                book.uploader = request.user
            book.revise(request.user, moderator)
            book.save()
            form.save_m2m()
            sync_files(request, book)
            # Модератор, публикуя своё новое или отклонённое, идёт МИМО book_review,
            # где награда и дописывается (то же было и у материалов).
            if book.is_published:
                rewards.sync(book.uploader)
                if request.user != book.uploader:
                    rewards.sync(request.user)

            # Своими же правками модерацию не дёргаем: книга и так лежит в её очереди.
            if book.is_pending and not moderator:
                notify(MODERATION, "telegram/book_pending.html", {
                    "book": book, "editor": request.user, "created": not pk,
                    "url": request.build_absolute_uri(book.get_absolute_url()),
                })
            if pk:
                messages.success(request, "Книга сохранена." if book.is_published else "Книга сохранена и ждёт проверки.")
            else:
                messages.success(request, "Книга добавлена." if book.is_published else "Книга добавлена и ждёт проверки.")
            return redirect("book_detail", pk=book.pk)

    # На htmx-запрос отдаём только форму: она сама себя и заменит вместе с ошибками.
    template = "library/_book_form.html" if request.headers.get("HX-Request") else "library/book_form.html"
    return render(request, template, {
        "form": form, "book": book, "file_errors": file_errors,
        "max_size_hint": human_size(max_upload_size(request.user)), "upload_limits": upload_limits(request.user),
        "saved_files": saved_files(book),
        "pending": pending_uploads(request) if request.method == "POST" else [],
    })


@require_POST
def book_delete(request, pk):
    book = get_object_or_404(visible(request.user), pk=pk)
    if not _may_edit(request.user, book):
        return HttpResponseForbidden("Удалить книгу может только тот, кто её добавил.")
    book.delete()  # файлы уедут каскадом, блобы снимет post_delete
    notify(MODERATION, "telegram/book_deleted.html", {"book": book, "editor": request.user})
    messages.success(request, "Книга удалена.")
    return redirect("book_list")


@require_POST
def book_review(request, pk):
    """Решение модератора. Отсюда и со страницы книги, и из общей очереди —
    отвечаем по-разному: очередь ждёт htmx-кусок, страница книги — редирект."""
    if not _may_moderate(request.user):
        return HttpResponseForbidden("Проверять книги может только модерация.")

    book = get_object_or_404(Book, pk=pk)
    if request.POST.get("decision") == "approve":
        book.approve(request.user)
        text = "Книга опубликована."
    else:
        book.reject(request.user, request.POST.get("note", ""))
        text = "Книга отклонена."
    book.save(update_fields=Book.REVIEW_FIELDS)
    # Автору — за опубликованную работу, модератору — за разобранную чужую.
    rewards.sync(book.uploader)
    rewards.sync(request.user)

    if request.headers.get("HX-Request"):
        return render(request, "moderation/_decision.html", {"text": text})
    messages.success(request, text)
    return redirect("book_detail", pk=book.pk)
