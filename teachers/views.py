from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from attachments.uploads import drop_replaced
from core.search import by_name
from economy import rewards

from .forms import ReviewForm
from .models import SCORE_LABELS, Review, Teacher

PAGE_SIZE = 20  # преподавателей в порции
# Сколько оценок нужно, чтобы попасть в топ. Без порога наверху висел человек с одним
# отзывом на пять звёзд: средняя от одной оценки — это не рейтинг, а мнение одного студента.
TOP_MIN_REVIEWS = 15
TOP_SIZE = 3


def _reviews(user):
    return Review.objects.select_related("author__team").annotate(
        likes=Count("liked_users", distinct=True),
        dislikes=Count("disliked_users", distinct=True),
        liked_by_me=Exists(Review.liked_users.through.objects.filter(review_id=OuterRef("pk"), user_id=user.pk)),
        disliked_by_me=Exists(Review.disliked_users.through.objects.filter(review_id=OuterRef("pk"), user_id=user.pk)),
    )


def _hx_refresh():
    # после действий, меняющих статистику в сайдбаре, перезагружаем страницу целиком
    response = HttpResponse()
    response["HX-Refresh"] = "true"
    return response


def _top(size=TOP_SIZE):
    """Лучшие — среди тех, кого оценивали достаточно много раз.

    Порог отсекает случайные пятёрки, а заодно оставляет в памяти десяток человек
    вместо всей кафедры: считать среднюю из четырёх шкал в Python по всему списку
    было бы вторым проходом по той же таблице.
    """
    rated = [
        teacher for teacher in Teacher.objects.with_ratings().filter(reviews_count__gte=TOP_MIN_REVIEWS)
        if teacher.overall_rating()
    ]
    return sorted(rated, key=lambda t: (t.overall_rating(), t.reviews_count), reverse=True)[:size]


def teacher_list(request):
    q = request.GET.get("q", "").strip()
    teachers = Teacher.objects.with_ratings().prefetch_related("subjects")
    if q:
        # Отчество здесь в поиске участвует: на карточке оно на виду, и ФИО целиком
        # переписывают из расписания.
        teachers = by_name(teachers, q, fields=("surname", "name", "patronymic"))

    # Порциями, как книги и материалы: у преподавателя есть фото, и каждое — отдельный
    # запрос за картинкой. Списком целиком страница открывалась минуту.
    page = Paginator(teachers, PAGE_SIZE).get_page(request.GET.get("page"))
    context = {"page": page, "teachers": page.object_list, "q": q}

    # Живой поиск и догрузка: HTMX перерисовывает только список.
    if request.headers.get("HX-Request"):
        return render(request, "teachers/_teacher_list.html", context)
    return render(request, "teachers/teacher_list.html", {**context, "top": _top()})


def teacher_detail(request, pk):
    teacher = get_object_or_404(Teacher.objects.with_ratings().prefetch_related("subjects"), pk=pk)
    user_review = teacher.reviews.filter(author=request.user).first()

    form = ReviewForm(request.POST or None, request.FILES or None, instance=user_review)
    if request.method == "POST" and form.is_valid():
        review = form.save(commit=False)
        review.teacher = teacher
        review.author = request.user
        review.save()
        drop_replaced(form)
        # Отзыв с текстом стоит дороже голых оценок, поэтому пересчёт нужен и на правке:
        # человек мог дописать текст к тому, что раньше было одними звёздами.
        rewards.sync(request.user)
        messages.success(request, "Отзыв сохранён")
        return redirect("teacher_detail", pk=pk)

    # лента: все отзывы; чужие без текста по умолчанию свёрнуты (Alpine showAll)
    reviews = (
        _reviews(request.user)
        .filter(teacher=teacher)
        .order_by("-created")  # annotate с GROUP BY отбрасывает Meta.ordering
    )
    hidden_count = sum(1 for r in reviews if not r.is_detailed() and r.author_id != request.user.pk)
    scales = [
        (SCORE_LABELS["score_knowledge"], teacher.avg_knowledge),
        (SCORE_LABELS["score_skill"], teacher.avg_skill),
        (SCORE_LABELS["score_communication"], teacher.avg_communication),
        (SCORE_LABELS["score_freeloading"], teacher.avg_freeloading),
    ]
    return render(request, "teachers/teacher_detail.html", {
        "teacher": teacher,
        "reviews": reviews,
        "user_review": user_review,
        "scales": scales,
        "hidden_count": hidden_count,
        "form": form,
    })


def review_card(request, pk):
    review = get_object_or_404(_reviews(request.user), pk=pk)
    return render(request, "teachers/_review.html", {"r": review})


def review_edit(request, pk):
    review = get_object_or_404(_reviews(request.user), pk=pk)
    if review.author_id != request.user.pk and not request.user.has_perm("teachers.change_review"):
        raise PermissionDenied
    form = ReviewForm(request.POST or None, request.FILES or None, instance=review)
    if request.method == "POST" and form.is_valid():
        form.save()
        drop_replaced(form)
        rewards.sync(review.author)
        messages.success(request, "Отзыв сохранён")
        return _hx_refresh()
    return render(request, "teachers/_review_form.html", {"form": form, "r": review})


@require_POST
def review_delete(request, pk):
    review = get_object_or_404(Review, pk=pk)
    if review.author_id != request.user.pk and not request.user.has_perm("teachers.delete_review"):
        raise PermissionDenied
    review.delete()
    messages.success(request, "Отзыв удалён")
    return _hx_refresh()


@require_POST
def review_vote(request, pk, vote):
    review = get_object_or_404(Review, pk=pk)
    mine, other = (review.liked_users, review.disliked_users) if vote == "like" else (review.disliked_users, review.liked_users)
    if mine.filter(pk=request.user.pk).exists():
        mine.remove(request.user)
    else:
        mine.add(request.user)
        other.remove(request.user)
    # Награда автору, не голосующему. Снятый лайк её не отнимает — как и удалённый
    # материал: sync только доначисляет, вниз пересчёта нет вовсе.
    rewards.sync(review.author)
    return review_card(request, pk)
