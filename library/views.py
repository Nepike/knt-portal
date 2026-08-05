from django.core.paginator import Paginator
from django.db.models import OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, render

from attachments.models import File

from .forms import BookFilterForm
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
    """Неодобренную книгу видит только тот, кто её принёс.
    TODO: показывать все модераторам — вместе с правом moderate_book"""
    return Book.objects.filter(Q(approved=True) | Q(uploader=user))


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
    return render(request, "library/book_detail.html", {"book": book})
