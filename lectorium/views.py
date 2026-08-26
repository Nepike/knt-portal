import posixpath

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from attachments.media import hls_url, redirect_url
from attachments.models import human_size
from attachments.storage import file_storage
from attachments.uploads import max_upload_size, upload_key, upload_limits
from intake.models import MediaJob
from intake.spec import POSTER

from .forms import LectureForm, PlaylistForm
from .models import Playlist

# Рецепт, по которому печётся всё, что сдают в лекторий.
LECTURE_RECIPE = "lecture"


def _visible(user):
    """Непроверенный плейлист видят только автор и модерация."""
    if _may_moderate(user):
        return Playlist.objects.all()
    return Playlist.objects.filter(Q(status=Playlist.Status.APPROVED) | Q(uploader=user))


def _may_moderate(user):
    return user.has_perm("lectorium.change_playlist")


def _may_add(user):
    """Заводить курсы и сдавать записи — по отдельному праву: лекции выкладывают
    не все подряд, а печь их дорого."""
    return user.has_perm("lectorium.add_playlist")


def _may_edit(user, playlist):
    return playlist.uploader_id == user.pk or _may_moderate(user)


def playlist_edit(request, pk=None):
    """Новый курс или правка существующего."""
    playlist = get_object_or_404(_visible(request.user), pk=pk) if pk else Playlist(uploader=request.user)
    if not (_may_add(request.user) if pk is None else _may_edit(request.user, playlist)):
        return HttpResponseForbidden("Заводить курсы лекций может не каждый.")

    form = PlaylistForm(request.POST or None, instance=playlist)
    if request.method == "POST" and form.is_valid():
        # Правка не-модератором возвращает курс в очередь: иначе одобренное можно было бы
        # тихо подменить. Ровно то же правило, что у материалов и книг.
        form.instance.revise(request.user, _may_moderate(request.user))
        form.save()
        messages.success(request, "Курс сохранён." if pk else "Курс заведён — добавь записи.")
        return redirect("playlist_detail", pk=form.instance.pk)

    return render(request, "lectorium/playlist_form.html", {"form": form, "playlist": playlist if pk else None})


def _source_problem(user, key):
    """Причина не принимать это сырьё, или None.

    Спрашиваем ХРАНИЛИЩЕ, а не браузер, и уже после заливки. Размер в подписанной ссылке
    не участвует: браузер объявляет его до отправки, а положить по ссылке может сколько
    угодно — это единственное место, где настоящий вес вообще становится известен.
    Заодно ловится недоехавший файл: иначе задание встало бы в очередь, и человек узнал
    бы о пропаже через час, из «не обработалась».
    """
    try:
        size = file_storage().size(key)
    except FileNotFoundError:
        return "Запись не доехала до хранилища — загрузи её заново."
    limit = max_upload_size(user)
    if size > limit:
        return f"Запись больше {human_size(limit)}."
    return None


@require_POST
def lecture_add(request, pk):
    """Сдать запись: файл уже в хранилище, здесь заводится лекция и задание на выпечку."""
    playlist = get_object_or_404(_visible(request.user), pk=pk)
    if not _may_edit(request.user, playlist):
        return HttpResponseForbidden("Добавлять записи может только автор курса или модерация.")

    form = LectureForm(request.POST)
    source = upload_key(request.POST.get("uploaded", ""))
    if not form.is_valid() or not source:
        messages.error(request, "Нужны название и файл записи.")
        return redirect("playlist_detail", pk=playlist.pk)

    if problem := _source_problem(request.user, source):
        messages.error(request, problem)
        return redirect("playlist_detail", pk=playlist.pk)

    with transaction.atomic():
        lecture = form.save(commit=False)
        lecture.playlist = playlist
        lecture.order = playlist.lectures.count()
        lecture.prefix = ""  # появится, когда пекарня отчитается
        lecture.save()
        MediaJob.objects.create(recipe=LECTURE_RECIPE, source=source, lecture=lecture)

    messages.success(request, "Запись принята — она встала в очередь на обработку.")
    return redirect("playlist_detail", pk=playlist.pk)


def playlist_list(request):
    playlists = (
        _visible(request.user)
        .select_related("subject", "uploader")
        .prefetch_related("terms", "teachers", "lectures")
        .annotate(lectures_count=Count("lectures"))
    )
    return render(request, "lectorium/playlists.html",
                  {"playlists": playlists, "may_add": _may_add(request.user)})


def playlist_detail(request, pk):
    playlist = get_object_or_404(
        # lectures__job — ради значка «не обработалась» в списке: без него состояние
        # спрашивалось бы отдельным запросом на каждую необработанную запись.
        _visible(request.user).select_related("subject", "uploader").prefetch_related(
            "terms", "teachers", "lectures", "lectures__job",
        ),
        pk=pk,
    )
    lectures = list(playlist.lectures.all())
    ready = [one for one in lectures if one.prefix]
    # Какую смотрим: по номеру из адреса, иначе первую готовую. Номер, а не порядковый
    # индекс — ссылку на лекцию можно переслать, и она не поедет от вставки новой
    # в середину. Только из готовых: у необработанной папки набора ещё нет, и плеер
    # получил бы адрес в никуда вместо честного «обрабатывается».
    chosen = next((one for one in ready if str(one.pk) == request.GET.get("lecture")), None)
    return render(request, "lectorium/playlist_detail.html", {
        "playlist": playlist,
        "lectures": lectures,
        "lecture": chosen or (ready[0] if ready else None),
        "may_moderate": _may_moderate(request.user),
        "may_edit": _may_edit(request.user, playlist),
        "lecture_form": LectureForm(),
        "upload_limits": upload_limits(request.user),
        "max_size_hint": max_upload_size(request.user),
    })


@require_POST
def playlist_review(request, pk):
    """Решение модератора: и со страницы плейлиста, и из общей очереди."""
    if not _may_moderate(request.user):
        return HttpResponseForbidden("Проверять лекции может только модерация.")

    playlist = get_object_or_404(Playlist, pk=pk)
    if request.POST.get("decision") == "approve":
        playlist.approve(request.user)
        text = "Плейлист опубликован."
    else:
        playlist.reject(request.user, request.POST.get("note", ""))
        text = "Плейлист отклонён."
    playlist.save(update_fields=Playlist.REVIEW_FIELDS)

    if request.headers.get("HX-Request"):
        return render(request, "moderation/_decision.html", {"text": text})
    messages.success(request, text)
    return redirect("playlist_detail", pk=playlist.pk)


def check(request):
    """Посмотреть набор HLS, лежащий в хранилище, по ключу его манифеста.

    Служебная: набор бывает залит, но ещё не привязан к лекции, — а другого способа
    убедиться, что он вообще играет, нет.
    """
    if not request.user.is_staff:
        return HttpResponseForbidden("Эта страница только для сотрудников.")

    key = request.GET.get("key", "").strip().lstrip("/")
    problem = manifest = poster = ""
    if key and not key.endswith(".m3u8"):
        problem = "Ключ должен указывать на манифест .m3u8"
    elif ".." in key.split("/"):
        # На диске такой ключ роняет хранилище SuspiciousFileOperation, то есть
        # пятисоткой вместо ответа. Ключ вводят руками, опечатка тут — обычное дело.
        problem = "Ключ — это путь внутри хранилища, без «..»"
    elif key and not file_storage().exists(key):
        problem = f"В хранилище нет такого куска: {key}"
    elif key:
        manifest = hls_url(key)
        beside = posixpath.join(posixpath.dirname(key), POSTER)
        if file_storage().exists(beside):
            poster = redirect_url(beside)

    return render(request, "lectorium/check.html",
                  {"key": key, "manifest": manifest, "poster": poster, "problem": problem})
