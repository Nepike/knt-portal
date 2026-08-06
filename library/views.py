from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from attachments.models import File, human_size
from attachments.uploads import (
    check_uploads, max_upload_size, pending_uploads, saved_files, sync_files, upload_limits,
)

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


def _visible(user):
    """Неодобренную книгу видят только принёсший её и модерация."""
    if user.has_perm("library.change_book"):
        return Book.objects.all()
    return Book.objects.filter(Q(approved=True) | Q(uploader=user))


def _may_edit(user, book):
    return book.uploader_id == user.pk or user.has_perm("library.change_book")


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
        _visible(request.user)
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
    }
    if request.headers.get("HX-Request"):
        return render(request, "library/_book_list.html", context)
    return render(request, "library/books.html", context)


def book_detail(request, pk):
    book = get_object_or_404(
        _visible(request.user).select_related("uploader").prefetch_related("subjects", "terms", "files"), pk=pk
    )
    return render(request, "library/book_detail.html", {"book": book, "may_edit": _may_edit(request.user, book)})


def _files_left(request, book):
    """Сколько файлов останется у книги после сохранения: непомеченные старые плюс новые."""
    kept = sum(1 for f in book.files.all() if not request.POST.get(f"delete-{f.pk}")) if book else 0
    return kept + len(request.FILES.getlist("files")) + len(pending_uploads(request))


def book_edit(request, pk=None):
    """Одна вьюха на создание и правку: поля и работа с файлами одинаковые."""
    book = get_object_or_404(_visible(request.user), pk=pk) if pk else None
    if book and not _may_edit(request.user, book):
        return HttpResponseForbidden("Эту книгу может редактировать только тот, кто её добавил.")

    form = BookForm(request.POST or None, instance=book)
    file_errors = []
    if request.method == "POST":
        file_errors = check_uploads(request.FILES.getlist("files"))
        # Книга — это и есть файл. Без него она бесполезна, в отличие от материала,
        # где есть текст и картинки.
        if not _files_left(request, book):
            file_errors.append("Добавь хотя бы один файл — без него книги не будет.")
        if form.is_valid() and not file_errors:
            book = form.save(commit=False)
            if not book.pk:
                book.uploader = request.user
                # Модератор публикует сразу, остальные ждут проверки.
                book.approved = request.user.has_perm("library.change_book")
            book.save()
            form.save_m2m()
            sync_files(request, book)
            if pk:
                messages.success(request, "Книга сохранена.")
            else:
                messages.success(request, "Книга добавлена." if book.approved else "Книга добавлена и ждёт проверки.")
            return redirect("book_detail", pk=book.pk)

    # На htmx-запрос отдаём только форму: она сама себя и заменит вместе с ошибками.
    template = "library/_book_form.html" if request.headers.get("HX-Request") else "library/book_form.html"
    return render(request, template, {
        "form": form, "book": book, "file_errors": file_errors,
        "max_size_hint": human_size(max_upload_size()), "upload_limits": upload_limits(),
        "saved_files": saved_files(book),
        "pending": pending_uploads(request) if request.method == "POST" else [],
    })


@require_POST
def book_delete(request, pk):
    book = get_object_or_404(_visible(request.user), pk=pk)
    if not _may_edit(request.user, book):
        return HttpResponseForbidden("Удалить книгу может только тот, кто её добавил.")
    book.delete()  # файлы уедут каскадом, блобы снимет post_delete
    messages.success(request, "Книга удалена.")
    return redirect("book_list")
