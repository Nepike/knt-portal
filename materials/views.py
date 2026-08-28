from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from attachments.models import human_size
from attachments.uploads import (
    MAX_IMAGE_SIZE, check_images, check_pending, check_uploads, drop_replaced, max_upload_size,
    pending_uploads, saved_files, saved_images, sync_files, sync_images, upload_limits,
)
from bookmarks.views import button as bookmark_button
from comments.views import context as comments_context
from core import filters
from economy import rewards
from telegram.notify import MODERATION, notify

from .forms import MaterialForm
from .models import Material

PAGE_SIZE = 20  # материалов в порции


def visible(user):
    """Неопубликованный материал видят только автор и модерация.

    Имя публичное: этим же запросом приложение комментариев проверяет, что человеку
    вообще положено видеть обсуждение — иначе по прямому адресу можно было бы
    открыть и лайкать чужой черновик.
    """
    if _may_moderate(user):
        return Material.objects.all()
    return Material.objects.filter(Q(status=Material.Status.APPROVED) | Q(uploader=user))


def _may_moderate(user):
    return user.has_perm("materials.change_material")


def _may_edit(user, material):
    return material.uploader_id == user.pk or _may_moderate(user)


def _list_url(params):
    """Адрес списка с выбранным подбором — со страницы материала есть куда вернуться."""
    query = filters.query(params)
    return f"{reverse('material_list')}?{query}" if query else reverse("material_list")


def material_list(request):
    form = filters.FilterForm(request.GET or None)

    materials = (
        visible(request.user)
        .select_related("subject", "uploader")
        .prefetch_related("terms", "teachers")
        .annotate(files_count=Count("files", distinct=True), images_count=Count("images", distinct=True))
    )
    chosen = filters.chosen(form)
    materials = filters.apply(materials, chosen)
    filters.narrow(form, visible(request.user), chosen)

    # Год здесь — год, когда материал был актуален, поэтому свежие сверху; id последним,
    # иначе на границе порций материалы с одинаковым ключом перескакивают.
    ordered = materials.distinct().order_by("-year", "-created", "-id")
    page = Paginator(ordered, PAGE_SIZE).get_page(request.GET.get("page"))

    context = {
        "page": page, "materials": page.object_list, "form": form,
        # Фильтры едут в ссылку каждой карточки — со страницы материала есть куда вернуться.
        "filters": filters.query(request.GET),
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
    response["HX-Push-Url"] = filters.url(request)
    return response


def material_detail(request, pk):
    material = get_object_or_404(
        visible(request.user).select_related("subject", "uploader").prefetch_related(
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
        # Кнопка закладки в шапке: она одна на весь сайт, а что помечать — знает страница.
        "bookmark": bookmark_button(request.user, material),
        # Лента — из приложения комментариев: она одна на материалы и лекции.
        **comments_context(request.user, material),
    })


def material_edit(request, pk=None):
    """Одна вьюха на создание и правку: поля и работа с файлами одинаковые."""
    material = get_object_or_404(visible(request.user), pk=pk) if pk else None
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
            # Модератор, публикуя своё новое или отклонённое, идёт МИМО material_review,
            # где награда и дописывается. Без этого вызова токены за работу ждали бы
            # следующего входа в систему — и человек решал бы, что их не дали вовсе.
            if material.is_published:
                rewards.sync(material.uploader)
                if request.user != material.uploader:
                    rewards.sync(request.user)  # ему — за разобранную чужую работу

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
        "max_size_hint": human_size(max_upload_size(request.user)), "upload_limits": upload_limits(request.user),
        "max_image_size": MAX_IMAGE_SIZE, "max_image_hint": human_size(MAX_IMAGE_SIZE),
        "saved_files": saved_files(material), "saved_images": saved_images(material),
        "pending": pending_uploads(request) if request.method == "POST" else [],
    })


@require_POST
def material_delete(request, pk):
    material = get_object_or_404(visible(request.user), pk=pk)
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
    # Автору — за опубликованную работу, модератору — за разобранную чужую.
    rewards.sync(material.uploader)
    rewards.sync(request.user)

    if request.headers.get("HX-Request"):
        return render(request, "moderation/_decision.html", {"text": text})
    messages.success(request, text)
    return redirect("material_detail", pk=material.pk)
