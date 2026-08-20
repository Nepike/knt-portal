from collections import defaultdict
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from attachments.models import human_size
from attachments.uploads import (
    MAX_IMAGE_SIZE, check_images, check_pending, check_uploads, drop_replaced, max_upload_size,
    pending_uploads, saved_files, saved_images, sync_files, sync_images, upload_limits,
)
from telegram.notify import MODERATION, notify

from .forms import CommentForm, MaterialFilterForm, MaterialForm
from .models import Comment, Material

PAGE_SIZE = 20  # материалов в порции
# Поля фильтра и то, как каждое цепляется к материалу.
FILTERS = {"term": "terms", "subject": "subject", "teacher": "teachers"}


def _visible(user):
    """Неопубликованный материал видят только автор и модерация."""
    if _may_moderate(user):
        return Material.objects.all()
    return Material.objects.filter(Q(status=Material.Status.APPROVED) | Q(uploader=user))


def _may_moderate(user):
    return user.has_perm("materials.change_material")


def _may_edit(user, material):
    return material.uploader_id == user.pk or _may_moderate(user)


def _filter_query(params):
    """Непустые фильтры строкой запроса. Из неё собирается и адрес списка, и ссылка
    «назад» на странице материала: без неё возврат к списку сбрасывал бы весь подбор."""
    return urlencode({name: value for name in FILTERS if (value := params.get(name))})


def _filters_url(request):
    """Адрес с текущими фильтрами: ссылку можно переслать, а F5 не сбросит подбор."""
    query = _filter_query(request.GET)
    return f"{request.path}?{query}" if query else request.path


def _list_url(params):
    query = _filter_query(params)
    return f"{reverse('material_list')}?{query}" if query else reverse("material_list")


def _narrow(form, base, chosen):
    """Оставить в каждом селекте только то, что вообще встречается у материалов,
    отобранных ОСТАЛЬНЫМИ фильтрами: выбрал семестр — предметы сузились до его предметов.

    Свой фильтр в расчёт не берём: иначе в списке осталось бы одно уже выбранное значение
    и сменить его было бы нечем. Выбранное на всякий случай добавляем к списку явно —
    семестр могли выбрать после предмета, которого в нём нет, и тогда своего же значения
    в списке не оказалось бы. Формы это не касается: она уже проверена по полным наборам,
    сужаем только то, что рисуется.
    """
    for name, lookup in FILTERS.items():
        rest = base
        for other, other_lookup in FILTERS.items():
            if other != name and chosen.get(other):
                rest = rest.filter(**{other_lookup: chosen[other]})
        found = Q(pk__in=rest.values(lookup))
        if chosen.get(name):
            found |= Q(pk=chosen[name].pk)
        field = form.fields[name]
        field.queryset = field.queryset.filter(found)


def material_list(request):
    form = MaterialFilterForm(request.GET or None)

    materials = (
        _visible(request.user)
        .select_related("subject", "uploader")
        .prefetch_related("terms", "teachers")
        .annotate(files_count=Count("files", distinct=True), images_count=Count("images", distinct=True))
    )
    chosen = {name: form.cleaned_data[name] for name in FILTERS} if form.is_valid() else {}
    for name, lookup in FILTERS.items():
        if chosen.get(name):
            materials = materials.filter(**{lookup: chosen[name]})
    _narrow(form, _visible(request.user), chosen)

    # Год здесь — год, когда материал был актуален, поэтому свежие сверху; id последним,
    # иначе на границе порций материалы с одинаковым ключом перескакивают.
    ordered = materials.distinct().order_by("-year", "-created", "-id")
    page = Paginator(ordered, PAGE_SIZE).get_page(request.GET.get("page"))

    context = {
        "page": page, "materials": page.object_list, "form": form,
        # Фильтры едут в ссылку каждой карточки — со страницы материала есть куда вернуться.
        "filters": _filter_query(request.GET),
        # Заголовок года не должен повториться на стыке порций: сравниваем с годом
        # элемента, стоящего прямо перед первым на этой странице.
        "carry_year": ordered[page.start_index() - 2].year if page.number > 1 else None,
    }
    if not request.headers.get("HX-Request"):
        return render(request, "materials/materials.html", context)

    if request.GET.get("page"):
        return render(request, "materials/_material_list.html", context)

    # Сменили фильтр — вместе со списком возвращаем и сам блок фильтров: наборы вариантов
    # в остальных селектах после этого другие.
    response = render(request, "materials/_material_list.html", {**context, "refresh_filters": True})
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
        # Ссылка «Материалы» ведёт не в начало списка, а туда, откуда пришли: фильтры
        # приезжают сюда в адресе карточки.
        "back_url": _list_url(request.GET),
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


def _comment(request, pk):
    """Комментарий — только если сам материал этому человеку виден: иначе через прямой
    адрес можно было бы лайкать и открывать обсуждение чужого черновика."""
    return get_object_or_404(Comment.objects.filter(material__in=_visible(request.user)), pk=pk)


def _may_touch(user, comment):
    return comment.author_id == user.pk or user.has_perm("materials.change_comment")


def _node(request, material, pk):
    """Комментарий, взятый ИЗ ЛЕНТЫ — то есть с прицепленными ответами и подписью
    адресата. Собирать карточку из голого объекта нельзя: у корня не оказалось бы
    ответов, и они пропадали бы с экрана от простого лайка."""
    for root in _thread(request.user, material):
        if root.pk == pk:
            return root
        for answer in root.answers:
            if answer.pk == pk:
                return answer
    return None


def _card(request, material, pk):
    comment = _node(request, material, pk)
    if comment is None:
        raise Http404
    # comment_form нужен форме ответа внутри карточки — она есть у каждого комментария.
    return render(request, "materials/_comment.html", {
        "c": comment, "material": material, "comment_form": CommentForm(),
    })


def _comments_block(request, material, form=None, parent=None):
    """Вся лента целиком. Перерисовывать её на каждое действие дешевле, чем вставлять
    ответ в нужную ветку точечно: комментариев десятки, а ошибиться местом — легко.

    Форма с ошибкой возвращается ровно туда, откуда её отправили: у ветки она приезжает
    прицепленной к своему комментарию (reply_form), иначе чужой текст оказался бы
    подставлен во все формы ответа разом.
    """
    comments = _thread(request.user, material)
    if form and parent:
        for node in (node for root in comments for node in (root, *root.answers)):
            if node.pk == parent:
                node.reply_form = form
    return render(request, "materials/_comments.html", {
        "material": material,
        "comments": comments,
        "total": sum(1 + len(root.answers) for root in comments),
        "comment_form": CommentForm() if parent else (form or CommentForm()),
    })


@require_POST
def comment_add(request, pk):
    material = get_object_or_404(_visible(request.user), pk=pk)
    form = CommentForm(request.POST, request.FILES)
    # Храним настоящего адресата; плоской ветку делает уже _thread при выводе.
    parent = Comment.objects.filter(pk=request.POST.get("parent") or 0, material=material).first()
    if not form.is_valid():
        return _comments_block(request, material, form, parent.pk if parent else None)

    comment = form.save(commit=False)
    comment.material = material
    comment.author = request.user
    comment.parent = parent
    comment.save()
    return _comments_block(request, material)


def comment_edit(request, pk):
    comment = _comment(request, pk)
    if not _may_touch(request.user, comment):
        return HttpResponseForbidden("Это чужой комментарий.")

    form = CommentForm(request.POST or None, request.FILES or None, instance=comment)
    if request.method == "POST" and form.is_valid():
        form.save()
        drop_replaced(form)
        return _card(request, comment.material, pk)
    return render(request, "materials/_comment_form.html", {
        "form": form, "c": comment, "material": comment.material,
    })


@require_POST
def comment_delete(request, pk):
    comment = _comment(request, pk)
    if not _may_touch(request.user, comment):
        return HttpResponseForbidden("Это чужой комментарий.")
    material = comment.material
    comment.delete()  # ответы уедут каскадом, картинку снимет post_delete
    # Отдаём ленту целиком, а не пустоту: вместе с комментарием исчезают его ответы
    # и меняется счётчик — точечным удалением узла этого не показать.
    return _comments_block(request, material)


@require_POST
def comment_vote(request, pk, vote):
    comment = _comment(request, pk)
    mine, other = (
        (comment.liked_users, comment.disliked_users) if vote == "like"
        else (comment.disliked_users, comment.liked_users)
    )
    if mine.filter(pk=request.user.pk).exists():
        mine.remove(request.user)  # повторный клик снимает голос
    else:
        mine.add(request.user)
        other.remove(request.user)
    return _card(request, comment.material, pk)


def material_edit(request, pk=None):
    """Одна вьюха на создание и правку: поля и работа с файлами одинаковые."""
    material = get_object_or_404(_visible(request.user), pk=pk) if pk else None
    if material and not _may_edit(request.user, material):
        return HttpResponseForbidden("Этот материал может редактировать только тот, кто его добавил.")

    form = MaterialForm(request.POST or None, instance=material)
    file_errors, image_errors = [], []
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
            material.revise(request.user, moderator)
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
        "max_image_size": MAX_IMAGE_SIZE, "max_image_hint": human_size(MAX_IMAGE_SIZE),
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
