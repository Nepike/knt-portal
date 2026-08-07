from collections import defaultdict
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from attachments.models import human_size
from attachments.uploads import (
    MAX_IMAGE_SIZE, check_images, check_pending, check_uploads, max_upload_size, pending_uploads,
    saved_files, saved_images, sync_files, sync_images, upload_limits,
)
from telegram.notify import MODERATION, notify

from .forms import CommentForm, MaterialFilterForm, MaterialForm
from .models import Comment, Material

PAGE_SIZE = 20  # материалов в порции


def _visible(user):
    """Неопубликованный материал видят только автор и модерация."""
    if _may_moderate(user):
        return Material.objects.all()
    return Material.objects.filter(Q(status=Material.Status.APPROVED) | Q(uploader=user))


def _may_moderate(user):
    return user.has_perm("materials.change_material")


def _may_edit(user, material):
    return material.uploader_id == user.pk or _may_moderate(user)


def _filters_url(request):
    """Адрес с текущими фильтрами: ссылку можно переслать, а F5 не сбросит подбор."""
    query = urlencode({key: value for key, value in request.GET.items() if value and key != "page"})
    return f"{request.path}?{query}" if query else request.path


def material_list(request):
    form = MaterialFilterForm(request.GET or None)
    q = request.GET.get("q", "").strip()

    materials = (
        _visible(request.user)
        .select_related("subject")
        .prefetch_related("terms", "teachers")
        .annotate(files_count=Count("files", distinct=True))
    )
    if q:
        materials = materials.filter(Q(title__icontains=q) | Q(synopsis__icontains=q))
    if form.is_valid():
        if subject := form.cleaned_data["subject"]:
            materials = materials.filter(subject=subject)
        if term := form.cleaned_data["term"]:
            materials = materials.filter(terms=term)
        if teacher := form.cleaned_data["teacher"]:
            materials = materials.filter(teachers=teacher)

    # Год здесь — год, когда материал был актуален, поэтому свежие сверху; id последним,
    # иначе на границе порций материалы с одинаковым ключом перескакивают.
    ordered = materials.distinct().order_by("-year", "-created", "-id")
    page = Paginator(ordered, PAGE_SIZE).get_page(request.GET.get("page"))

    context = {
        "page": page, "materials": page.object_list, "q": q, "form": form,
        # Заголовок года не должен повториться на стыке порций: сравниваем с годом
        # элемента, стоящего прямо перед первым на этой странице.
        "carry_year": ordered[page.start_index() - 2].year if page.number > 1 else None,
    }
    if not request.headers.get("HX-Request"):
        return render(request, "materials/materials.html", context)

    response = render(request, "materials/_material_list.html", context)
    if not request.GET.get("page"):
        response["HX-Push-Url"] = _filters_url(request)
    return response


def material_detail(request, pk):
    material = get_object_or_404(
        _visible(request.user).select_related("subject", "uploader").prefetch_related(
            "terms", "teachers", "files", "images",
        ),
        pk=pk,
    )
    return render(request, "materials/material_detail.html", {
        "material": material,
        "may_edit": _may_edit(request.user, material),
        "may_moderate": _may_moderate(request.user),
        "comments": _thread(request.user, material),
        "comment_form": CommentForm(),
    })


# ── комментарии ───────────────────────────────────────────────────────────────
def _comments(user):
    """Лента со счётчиками лайков и отметкой «я уже голосовал» — одним запросом."""
    return Comment.objects.select_related("author__team").annotate(
        likes=Count("liked_users", distinct=True),
        dislikes=Count("disliked_users", distinct=True),
        liked_by_me=Exists(Comment.liked_users.through.objects.filter(comment_id=OuterRef("pk"), user_id=user.pk)),
        disliked_by_me=Exists(Comment.disliked_users.through.objects.filter(comment_id=OuterRef("pk"), user_id=user.pk)),
    )


def _thread(user, material):
    """Верхний уровень с прицепленными ответами.

    В базе дерево настоящее: parent — тот комментарий, на который отвечали. А на экране
    ветка плоская, ответы всех уровней лежат под своим корнем; кому именно отвечали,
    показывает подпись (addressee). Иначе на третьем уровне лента уезжает вправо
    и с телефона её не прочитать.
    """
    items = list(_comments(user).filter(material=material).order_by("created"))
    known = {comment.pk: comment for comment in items}
    answers = defaultdict(list)
    roots = []
    for comment in items:
        root = comment
        while root.parent_id in known:
            root = known[root.parent_id]
        if root is comment:
            roots.append(comment)
            continue
        # Адресат нужен, только если отвечали не самому корню — иначе и так понятно.
        comment.addressee = known[comment.parent_id] if comment.parent_id != root.pk else None
        answers[root.pk].append(comment)

    for comment in roots:
        comment.answers = answers.get(comment.pk, [])
    return roots


def _may_touch(user, comment):
    return comment.author_id == user.pk or user.has_perm("materials.change_comment")


def _card(request, comment):
    # comment_form нужен форме ответа внутри карточки — она есть у каждого комментария.
    return render(request, "materials/_comment.html", {
        "c": comment, "material": comment.material, "comment_form": CommentForm(),
    })


def _comments_block(request, material, form=None):
    """Вся лента целиком. Перерисовывать её на каждое действие дешевле, чем вставлять
    ответ в нужную ветку точечно: комментариев десятки, а ошибиться местом — легко."""
    return render(request, "materials/_comments.html", {
        "material": material,
        "comments": _thread(request.user, material),
        "comment_form": form or CommentForm(),
    })


@require_POST
def comment_add(request, pk):
    material = get_object_or_404(_visible(request.user), pk=pk)
    form = CommentForm(request.POST, request.FILES)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.material = material
        comment.author = request.user
        # Храним настоящего адресата; плоской ветку делает уже _thread при выводе.
        comment.parent = Comment.objects.filter(pk=request.POST.get("parent") or 0, material=material).first()
        comment.save()
        form = None
    return _comments_block(request, material, form)


def comment_edit(request, pk):
    comment = get_object_or_404(Comment, pk=pk)
    if not _may_touch(request.user, comment):
        return HttpResponseForbidden("Это чужой комментарий.")

    form = CommentForm(request.POST or None, request.FILES or None, instance=comment)
    if request.method == "POST" and form.is_valid():
        form.save()
        return _card(request, _comments(request.user).get(pk=pk))
    return render(request, "materials/_comment_form.html", {
        "form": form, "c": comment, "material": comment.material,
    })


@require_POST
def comment_delete(request, pk):
    comment = get_object_or_404(Comment, pk=pk)
    if not _may_touch(request.user, comment):
        return HttpResponseForbidden("Это чужой комментарий.")
    material = comment.material
    comment.delete()  # ответы уедут каскадом, картинку снимет post_delete
    # Отдаём ленту целиком, а не пустоту: вместе с комментарием исчезают его ответы
    # и меняется счётчик — точечным удалением узла этого не показать.
    return _comments_block(request, material)


@require_POST
def comment_vote(request, pk, vote):
    comment = get_object_or_404(Comment, pk=pk)
    mine, other = (
        (comment.liked_users, comment.disliked_users) if vote == "like"
        else (comment.disliked_users, comment.liked_users)
    )
    if mine.filter(pk=request.user.pk).exists():
        mine.remove(request.user)  # повторный клик снимает голос
    else:
        mine.add(request.user)
        other.remove(request.user)
    return _card(request, _comments(request.user).get(pk=pk))


def material_edit(request, pk=None):
    """Одна вьюха на создание и правку: поля и работа с файлами одинаковые."""
    material = get_object_or_404(_visible(request.user), pk=pk) if pk else None
    if material and not _may_edit(request.user, material):
        return HttpResponseForbidden("Этот материал может редактировать только тот, кто его добавил.")

    form = MaterialForm(request.POST or None, instance=material)
    file_errors = image_errors = []
    if request.method == "POST":
        # Файлов может не быть вовсе — в отличие от книги, у материала есть текст.
        file_errors = check_uploads(request.FILES.getlist("files")) + check_pending(request)
        image_errors = check_images(request.FILES.getlist("images"))
        if form.is_valid() and not file_errors and not image_errors:
            material = form.save(commit=False)
            new = not material.pk
            moderator = _may_moderate(request.user)
            if new:
                material.uploader = request.user

            if not moderator:
                material.send_to_review()
            elif new or material.status == Material.Status.REJECTED:
                material.approve(request.user)

            material.save()
            form.save_m2m()
            sync_files(request, material)
            sync_images(request, material)

            if material.is_pending and not moderator:
                notify(MODERATION, "telegram/material_pending.html", {
                    "material": material, "editor": request.user, "created": new,
                    "url": request.build_absolute_uri(material.get_absolute_url()),
                })
            if material.is_published:
                messages.success(request, "Материал сохранён." if pk else "Материал добавлен.")
            else:
                messages.success(request, "Материал сохранён и ждёт проверки." if pk else "Материал добавлен и ждёт проверки.")
            return redirect("material_detail", pk=material.pk)

    # На htmx-запрос отдаём только форму: она сама себя и заменит вместе с ошибками.
    template = "materials/_material_form.html" if request.headers.get("HX-Request") else "materials/material_form.html"
    return render(request, template, {
        "form": form, "material": material,
        "file_errors": file_errors, "image_errors": image_errors,
        "max_size_hint": human_size(max_upload_size()), "upload_limits": upload_limits(),
        "max_image_hint": human_size(MAX_IMAGE_SIZE),
        "saved_files": saved_files(material), "saved_images": saved_images(material),
        "pending": pending_uploads(request) if request.method == "POST" else [],
    })


@require_POST
def material_delete(request, pk):
    material = get_object_or_404(_visible(request.user), pk=pk)
    if not _may_edit(request.user, material):
        return HttpResponseForbidden("Удалить материал может только тот, кто его добавил.")
    material.delete()  # файлы и картинки уедут каскадом, блобы снимет post_delete
    notify(MODERATION, "telegram/material_deleted.html", {"material": material, "editor": request.user})
    messages.success(request, "Материал удалён.")
    return redirect("material_list")


@require_POST
def material_review(request, pk):
    """Решение модератора: и со страницы материала, и из общей очереди."""
    if not _may_moderate(request.user):
        return HttpResponseForbidden("Проверять материалы может только модерация.")

    material = get_object_or_404(Material, pk=pk)
    if request.POST.get("decision") == "approve":
        material.approve(request.user)
        text = "Материал опубликован."
    else:
        material.reject(request.user, request.POST.get("note", ""))
        text = "Материал отклонён."
    material.save(update_fields=Material.REVIEW_FIELDS)

    if request.headers.get("HX-Request"):
        return render(request, "moderation/_decision.html", {"text": text})
    messages.success(request, text)
    return redirect("material_detail", pk=material.pk)
