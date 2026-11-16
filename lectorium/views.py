import posixpath

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from attachments.media import hls_url, image_url
from attachments.models import human_size
from attachments.storage import file_storage
from attachments.uploads import max_upload_size, upload_key, upload_limits
from bookmarks.views import button as bookmark_button
from comments.views import context as comments_context
from core import filters
from core.throttle import throttled
from economy import rewards
from intake.models import MediaJob
from intake.spec import POSTER
from notifications import services as notifications
from telegram.notify import MODERATION, notify

from .forms import LectureForm, PlaylistForm
from .models import Lecture, Playlist

# Рецепт, по которому печётся всё, что сдают в лекторий.
LECTURE_RECIPE = "lecture"
# Курсов в порции: делится на 2 и на 3 — сетка добирается до конца ряда.
PAGE_SIZE = 24
# Записей за раз: сырьё лежит в платном бакете, пока до него не дойдёт пекарня.
BATCH = 10
# Записей в час с человека: пачку не стесняет, зациклившийся скрипт остановит.
ADD_LIMIT = 60


def visible_playlists(user):
    """Непроверенный плейлист видят только автор и модерация."""
    if _may_moderate(user):
        return Playlist.objects.all()
    return Playlist.objects.filter(Q(status=Playlist.Status.APPROVED) | Q(uploader=user))


def visible_lectures(user):
    """Записи, чей курс человеку виден. Имя публичное: им же comments проверяет, что
    обсуждение положено открывать."""
    return Lecture.objects.filter(playlist__in=visible_playlists(user))


def _with_votes(user, lectures):
    """Счётчики оценок и отметка «я уже голосовал» — одним запросом, как у комментариев."""
    return lectures.annotate(
        likes=Count("liked_users", distinct=True),
        dislikes=Count("disliked_users", distinct=True),
        liked_by_me=Exists(Lecture.liked_users.through.objects.filter(
            lecture_id=OuterRef("pk"), user_id=user.pk)),
        disliked_by_me=Exists(Lecture.disliked_users.through.objects.filter(
            lecture_id=OuterRef("pk"), user_id=user.pk)),
    )


def _may_moderate(user):
    return user.has_perm("lectorium.change_playlist")


def _may_add(user):
    """Курсы и записи — по отдельному праву: печь лекции дорого."""
    return user.has_perm("lectorium.add_playlist")


def _may_edit(user, playlist):
    return playlist.uploader_id == user.pk or _may_moderate(user)


def _list_url(params):
    """Адрес списка с выбранным подбором — со страницы курса есть куда вернуться."""
    query = filters.query(params, search=True)
    return f"{reverse('playlist_list')}?{query}" if query else reverse("playlist_list")


def _sync_lectures(request, playlist):
    """Переименование, порядок и удаление записей — на форме курса, как файлы материала
    (`attachments.uploads.sync_files`). Наборы и сырьё снимают сигналы (`intake.models`)."""
    for lecture in playlist.lectures.all():
        if request.POST.get(f"delete-{lecture.pk}"):
            lecture.delete()
            continue
        title = request.POST.get(f"name-{lecture.pk}", "").strip()
        if title and title != lecture.title:
            lecture.title = title[:150]
            lecture.save(update_fields=["title"])

    # Порядок — по скрытым input name="order"; удалённой записи в by_pk уже нет.
    by_pk = {lecture.pk: lecture for lecture in playlist.lectures.all()}
    for index, pk in enumerate(request.POST.getlist("order")):
        lecture = by_pk.get(int(pk)) if pk.isdigit() else None
        if lecture and lecture.order != index:
            lecture.order = index
            lecture.save(update_fields=["order"])


def playlist_edit(request, pk=None):
    """Новый курс или правка существующего."""
    playlist = get_object_or_404(visible_playlists(request.user), pk=pk) if pk else Playlist(uploader=request.user)
    if not (_may_add(request.user) if pk is None else _may_edit(request.user, playlist)):
        return HttpResponseForbidden("Заводить курсы лекций может не каждый.")

    form = PlaylistForm(request.POST or None, instance=playlist)
    if request.method == "POST" and form.is_valid():
        # Правка не-модератором возвращает курс на проверку.
        moderator = _may_moderate(request.user)
        form.instance.revise(request.user, moderator)
        form.save()
        if pk:
            _sync_lectures(request, playlist)
        # Модератор, публикуя своё, идёт мимо playlist_review — награду дописываем здесь.
        if form.instance.is_published:
            rewards.sync(form.instance.uploader)
            if request.user != form.instance.uploader:
                rewards.sync(request.user)  # ему — за разобранную чужую работу
        # Модерации — в чат: курс ждёт проверки неделями. О своём модератору не пишем.
        if form.instance.is_pending and not moderator:
            notify(MODERATION, "telegram/playlist_pending.html", {
                "playlist": form.instance, "editor": request.user, "created": pk is None,
                "url": request.build_absolute_uri(form.instance.get_absolute_url()),
            })
        messages.success(request, "Курс сохранён." if pk else "Курс заведён — добавь записи.")
        return redirect("playlist_detail", pk=form.instance.pk)

    return render(request, "lectorium/playlist_form.html", {
        "form": form,
        "playlist": playlist if pk else None,
        "lectures": list(playlist.lectures.select_related("job")) if pk else [],
        "refund": rewards.at_stake(playlist, request.user) if pk else 0,
    })


@require_POST
def playlist_delete(request, pk):
    """Снять курс целиком: записи уедут каскадом, наборы — из хранилища."""
    playlist = get_object_or_404(visible_playlists(request.user), pk=pk)
    if not _may_edit(request.user, playlist):
        return HttpResponseForbidden("Удалить курс может только тот, кто его завёл.")

    # Считаем ДО удаления: потом у объекта нет ни номера, ни записей.
    lectures = playlist.lectures.count()
    taken = rewards.remove(playlist, by=request.user)
    notify(MODERATION, "telegram/playlist_deleted.html",
           {"playlist": playlist, "editor": request.user, "lectures": lectures})
    messages.success(request, "Курс удалён." + rewards.taken_note(taken))
    return redirect("playlist_list")


def _source_problem(user, key):
    """Причина не принимать сырьё, или None — по ХРАНИЛИЩУ, после заливки: настоящий вес
    виден только тут, и ловится недоехавший файл."""
    try:
        size = file_storage().size(key)
    except FileNotFoundError:
        return "Запись не доехала до хранилища — загрузи её заново."
    limit = max_upload_size(user)
    if size > limit:
        return f"Запись больше {human_size(limit)}."
    return None


def _job_of(source):
    """Задание, которому это сырьё уже отдано, или None."""
    return MediaJob.objects.filter(source=source).select_related("lecture").first()


@require_POST
def lecture_add(request, pk):
    """Сдать ОДНУ запись (файл уже в хранилище): лекция и задание. Пачка шлёт сюда запрос на
    файл; ей отказ — строкой, обычной форме — сообщением на странице."""
    playlist = get_object_or_404(visible_playlists(request.user), pk=pk)
    if not _may_edit(request.user, playlist):
        return HttpResponseForbidden("Добавлять записи может только автор курса или модерация.")

    as_json = request.headers.get("Accept") == "application/json"

    def refuse(reason):
        if as_json:
            return JsonResponse({"error": reason}, status=400)
        messages.error(request, reason)
        return redirect("playlist_detail", pk=playlist.pk)

    if throttled(f"lecture:add:{request.user.pk}", ADD_LIMIT, 3600):
        return refuse("Слишком много записей за раз — попробуй позже.")

    form = LectureForm(request.POST)
    source = upload_key(request.POST.get("uploaded", ""))
    if not form.is_valid() or not source:
        return refuse("Нужны название и файл записи.")

    # Повтор сдачи (ответ потерялся): вторую лекцию на то же сырьё не заводим. До вопроса
    # хранилищу — к повтору сырья может уже не быть.
    job = _job_of(source)
    if job is None:
        if problem := _source_problem(request.user, source):
            return refuse(problem)
        try:
            with transaction.atomic():
                lecture = form.save(commit=False)
                lecture.playlist = playlist
                lecture.order = playlist.lectures.count()
                lecture.prefix = ""  # появится, когда пекарня отчитается
                lecture.save()
                MediaJob.objects.create(recipe=LECTURE_RECIPE, source=source, lecture=lecture)
        except IntegrityError:
            job = _job_of(source)  # два запроса с одним файлом пришли разом — победил другой
            if job is None:
                raise
    if job is not None:
        if job.lecture is None or job.lecture.playlist_id != playlist.pk:
            return refuse("Эта запись уже сдана в другой курс.")
        lecture = job.lecture

    if as_json:
        return JsonResponse({"title": lecture.title})
    messages.success(request, "Запись принята — она встала в очередь на обработку.")
    return redirect("playlist_detail", pk=playlist.pk)


def playlist_list(request):
    form = filters.FilterForm(request.GET or None)
    q = filters.asked(request.GET)

    # Найденное по названию — основа и списка, и вариантов в селектах.
    found = filters.search(visible_playlists(request.user), q)
    playlists = (
        found
        .select_related("subject", "uploader")
        .prefetch_related("terms", "teachers", "lectures")
        # distinct: join по многие-ко-многим удвоил бы счёт записей.
        .annotate(lectures_count=Count("lectures", distinct=True))
    )
    chosen = filters.chosen(form)
    playlists = filters.apply(playlists, chosen)
    filters.narrow(form, found, chosen)

    # Свежие сверху; id последним — иначе на границе порций курсы перескакивают.
    ordered = playlists.distinct().order_by("-year", "-created", "-id")
    page = Paginator(ordered, PAGE_SIZE).get_page(request.GET.get("page"))

    context = {
        "page": page, "playlists": page.object_list, "form": form, "q": q,
        # Подбор — в ссылки карточек: со страницы курса есть куда вернуться.
        "filters": filters.query(request.GET, search=True),
        "may_add": _may_add(request.user),
        # Год прошлой порции — чтобы заголовок не повторился на стыке.
        "carry_year": ordered[page.start_index() - 2].year if page.number > 1 else None,
    }
    if not request.headers.get("HX-Request"):
        return render(request, "lectorium/playlists.html", context)

    if request.GET.get("page"):
        return render(request, "lectorium/_playlist_list.html", context)

    # Сменили фильтр — возвращаем и блок фильтров: варианты другие.
    response = render(request, "lectorium/_playlist_list.html", {**context, "refresh_filters": True})
    response["HX-Push-Url"] = filters.url(request, search=True)
    return response


def playlist_detail(request, pk):
    playlist = get_object_or_404(
        # lectures__job — ради значка «не обработалась» без запроса на запись.
        visible_playlists(request.user).select_related("subject", "uploader").prefetch_related(
            "terms", "teachers", "lectures", "lectures__job",
        ),
        pk=pk,
    )
    lectures = list(playlist.lectures.all())
    ready = [one for one in lectures if one.prefix]
    # Какую смотрим: по номеру из адреса (ссылку пересылают), иначе первую готовую.
    chosen = next((one for one in ready if str(one.pk) == request.GET.get("lecture")), None)
    watching = chosen or (ready[0] if ready else None)
    # Открытую запись — со счётчиками оценок, отдельным запросом: в списке оценок нет.
    discussion = {}
    if watching is not None:
        watching = _with_votes(request.user, Lecture.objects.filter(pk=watching.pk)).get()
        discussion = comments_context(request.user, watching)
    notifications.seen_course(request.user, playlist, watching)

    return render(request, "lectorium/playlist_detail.html", {
        "playlist": playlist,
        "lectures": lectures,
        "lecture": watching,
        **discussion,
        # back_url — туда, откуда пришли (подбор едет в адресе карточки и в ссылках на записи).
        # bookmark — кнопка в шапке: что помечать, знает страница.
        "bookmark": bookmark_button(request.user, playlist),
        "back_url": _list_url(request.GET),
        "filters": filters.query(request.GET, search=True),
        "may_moderate": _may_moderate(request.user),
        "may_edit": _may_edit(request.user, playlist),
        "upload_limits": upload_limits(request.user),
        "max_size_hint": human_size(max_upload_size(request.user)),
        "batch": BATCH,
    })


@require_POST
def lecture_vote(request, pk, vote):
    """Оценка записи — как голос за комментарий: повторный клик снимает, противоположный переносит."""
    lecture = get_object_or_404(visible_lectures(request.user), pk=pk)
    mine, other = (
        (lecture.liked_users, lecture.disliked_users) if vote == "like"
        else (lecture.disliked_users, lecture.liked_users)
    )
    if mine.filter(pk=request.user.pk).exists():
        mine.remove(request.user)
    else:
        mine.add(request.user)
        other.remove(request.user)

    counted = _with_votes(request.user, Lecture.objects.filter(pk=pk)).get()
    return render(request, "lectorium/_reactions.html", {"lecture": counted})


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
    notifications.reviewed(playlist, request.user)
    # Автору — за опубликованный курс, модератору — за разобранную чужую работу.
    rewards.sync(playlist.uploader)
    rewards.sync(request.user)

    if request.headers.get("HX-Request"):
        return render(request, "moderation/_decision.html", {"text": text})
    messages.success(request, text)
    return redirect("playlist_detail", pk=playlist.pk)


def check(request):
    """Посмотреть набор HLS в хранилище по ключу манифеста — пока он не привязан к лекции."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Эта страница только для сотрудников.")

    key = request.GET.get("key", "").strip().lstrip("/")
    problem = manifest = poster = ""
    if key and not key.endswith(".m3u8"):
        problem = "Ключ должен указывать на манифест .m3u8"
    elif ".." in key.split("/"):
        # На диске такой ключ — SuspiciousFileOperation и 500; ключ вводят руками.
        problem = "Ключ — это путь внутри хранилища, без «..»"
    elif key and not file_storage().exists(key):
        problem = f"В хранилище нет такого куска: {key}"
    elif key:
        manifest = hls_url(key)
        beside = posixpath.join(posixpath.dirname(key), POSTER)
        if file_storage().exists(beside):
            poster = image_url(beside)

    return render(request, "lectorium/check.html",
                  {"key": key, "manifest": manifest, "poster": poster, "problem": problem})
