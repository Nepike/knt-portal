import posixpath

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from attachments.media import hls_url, redirect_url
from attachments.models import human_size
from attachments.storage import file_storage
from attachments.uploads import max_upload_size, upload_key, upload_limits
from bookmarks.views import button as bookmark_button
from comments.views import context as comments_context
from core import filters
from economy import rewards
from intake.models import MediaJob
from intake.spec import POSTER
from telegram.notify import MODERATION, notify

from .forms import LectureForm, PlaylistForm
from .models import Lecture, Playlist

# Рецепт, по которому печётся всё, что сдают в лекторий.
LECTURE_RECIPE = "lecture"
# Курсов в порции. Делится и на 2, и на 3 — на любой ширине сетка добирается до конца
# ряда, а не обрывается посередине.
PAGE_SIZE = 24


def visible_playlists(user):
    """Непроверенный плейлист видят только автор и модерация."""
    if _may_moderate(user):
        return Playlist.objects.all()
    return Playlist.objects.filter(Q(status=Playlist.Status.APPROVED) | Q(uploader=user))


def visible_lectures(user):
    """Записи, чей курс этому человеку виден.

    Имя публичное: этим же запросом приложение комментариев проверяет, что обсуждение
    вообще положено открывать, — иначе по прямому адресу можно было бы читать и лайкать
    непроверенный чужой курс.
    """
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
    """Заводить курсы и сдавать записи — по отдельному праву: лекции выкладывают
    не все подряд, а печь их дорого."""
    return user.has_perm("lectorium.add_playlist")


def _may_edit(user, playlist):
    return playlist.uploader_id == user.pk or _may_moderate(user)


def _list_url(params):
    """Адрес списка с выбранным подбором — со страницы курса есть куда вернуться."""
    query = filters.query(params)
    return f"{reverse('playlist_list')}?{query}" if query else reverse("playlist_list")


def _sync_lectures(request, playlist):
    """Переименование, порядок и удаление записей — на той же форме, что и сам курс.

    Своей страницы у записи нет и не нужно: правится в ней одно название. Ровно так же
    живут файлы материала (`attachments.uploads.sync_files`) — там же взяты и имена полей.
    Удаление снимает набор из хранилища сигналом, а недолитое и сырьё — сигналом на
    задании (`intake.models`).
    """
    for lecture in playlist.lectures.all():
        if request.POST.get(f"delete-{lecture.pk}"):
            lecture.delete()
            continue
        title = request.POST.get(f"name-{lecture.pk}", "").strip()
        if title and title != lecture.title:
            lecture.title = title[:150]
            lecture.save(update_fields=["title"])

    # Порядок на экране: перетаскивание переставляет сами строки, а вместе с ними едут
    # и скрытые input name="order" — браузер отправляет поля в порядке разметки.
    # Удалённая запись в присланном списке ещё встречается, её просто не будет в by_pk.
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
        # Правка не-модератором возвращает курс в очередь: иначе одобренное можно было бы
        # тихо подменить. Ровно то же правило, что у материалов и книг.
        moderator = _may_moderate(request.user)
        form.instance.revise(request.user, moderator)
        form.save()
        if pk:
            _sync_lectures(request, playlist)
        # Модератор, публикуя своё, идёт МИМО playlist_review, где награда и дописывается.
        # Без этого вызова токены за работу ждали бы следующего входа в систему —
        # и человек решал бы, что их не дали вовсе.
        if form.instance.is_published:
            rewards.sync(form.instance.uploader)
            if request.user != form.instance.uploader:
                rewards.sync(request.user)  # ему — за разобранную чужую работу
        # Модерации — как о материале и книге: очередь на сайте есть, но в неё надо зайти,
        # а курс лекций ждать проверки может неделями. Своё модератор публикует сам,
        # и сообщать ему о собственной работе незачем.
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
    })


@require_POST
def playlist_delete(request, pk):
    """Снять курс целиком. Записи уедут каскадом, а с ними — и наборы из хранилища:
    у курса из двадцати лекций это десятки гигабайт, которые иначе некому найти."""
    playlist = get_object_or_404(visible_playlists(request.user), pk=pk)
    if not _may_edit(request.user, playlist):
        return HttpResponseForbidden("Удалить курс может только тот, кто его завёл.")

    # Считаем ДО удаления: после него у объекта в памяти уже нет ни номера, ни записей,
    # а модерации важно, сколько работы исчезло.
    lectures = playlist.lectures.count()
    playlist.delete()
    notify(MODERATION, "telegram/playlist_deleted.html",
           {"playlist": playlist, "editor": request.user, "lectures": lectures})
    messages.success(request, "Курс удалён.")
    return redirect("playlist_list")


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
    playlist = get_object_or_404(visible_playlists(request.user), pk=pk)
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
    form = filters.FilterForm(request.GET or None)

    playlists = (
        visible_playlists(request.user)
        .select_related("subject", "uploader")
        .prefetch_related("terms", "teachers", "lectures")
        # distinct обязателен: подбор по преподавателю или семестру — это join
        # по многие-ко-многим, и без него курс с двумя лекторами насчитал бы себе
        # вдвое больше записей, чем в нём есть.
        .annotate(lectures_count=Count("lectures", distinct=True))
    )
    chosen = filters.chosen(form)
    playlists = filters.apply(playlists, chosen)
    filters.narrow(form, visible_playlists(request.user), chosen)

    # Год здесь — год, когда курс читали, поэтому свежие сверху; id последним, иначе
    # на границе порций курсы с одинаковым ключом перескакивают. Тем же порядком
    # список разбивается на годы в шаблоне: regroup собирает только идущие подряд.
    ordered = playlists.distinct().order_by("-year", "-created", "-id")
    page = Paginator(ordered, PAGE_SIZE).get_page(request.GET.get("page"))

    context = {
        "page": page, "playlists": page.object_list, "form": form,
        # Подбор едет в ссылку каждой карточки — со страницы курса есть куда вернуться.
        "filters": filters.query(request.GET),
        "may_add": _may_add(request.user),
        # Заголовок года не должен повториться на стыке порций: сравниваем с годом
        # курса, стоящего прямо перед первым на этой странице.
        "carry_year": ordered[page.start_index() - 2].year if page.number > 1 else None,
    }
    if not request.headers.get("HX-Request"):
        return render(request, "lectorium/playlists.html", context)

    if request.GET.get("page"):
        return render(request, "lectorium/_playlist_list.html", context)

    # Сменили фильтр — вместе со списком возвращаем и сам блок фильтров: наборы вариантов
    # в остальных селектах после этого другие.
    response = render(request, "lectorium/_playlist_list.html", {**context, "refresh_filters": True})
    response["HX-Push-Url"] = filters.url(request)
    return response


def playlist_detail(request, pk):
    playlist = get_object_or_404(
        # lectures__job — ради значка «не обработалась» в списке: без него состояние
        # спрашивалось бы отдельным запросом на каждую необработанную запись.
        visible_playlists(request.user).select_related("subject", "uploader").prefetch_related(
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
    watching = chosen or (ready[0] if ready else None)
    # Открытую запись перечитываем со счётчиками оценок. Отдельным запросом, а не
    # аннотацией на весь список: в списке оценки не показываются, и считать их
    # на каждую из двадцати записей незачем.
    discussion = {}
    if watching is not None:
        watching = _with_votes(request.user, Lecture.objects.filter(pk=watching.pk)).get()
        discussion = comments_context(request.user, watching)

    return render(request, "lectorium/playlist_detail.html", {
        "playlist": playlist,
        "lectures": lectures,
        "lecture": watching,
        **discussion,
        # Ссылка «Лекторий» ведёт не в начало списка, а туда, откуда пришли: подбор
        # приезжает сюда в адресе карточки. Он же цепляется к ссылкам на записи —
        # иначе первый же клик по записи стёр бы его из адреса.
        # Кнопка закладки в шапке: она одна на весь сайт, а что помечать — знает страница.
        "bookmark": bookmark_button(request.user, playlist),
        "back_url": _list_url(request.GET),
        "filters": filters.query(request.GET),
        "may_moderate": _may_moderate(request.user),
        "may_edit": _may_edit(request.user, playlist),
        "lecture_form": LectureForm(),
        "upload_limits": upload_limits(request.user),
        "max_size_hint": max_upload_size(request.user),
    })


@require_POST
def lecture_vote(request, pk, vote):
    """Оценка записи. Устроено ровно как голос за комментарий: повторный клик снимает,
    противоположный переносит — двух голосов от одного человека быть не может."""
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
    # Автору — за опубликованный курс, модератору — за разобранную чужую работу.
    rewards.sync(playlist.uploader)
    rewards.sync(request.user)

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
