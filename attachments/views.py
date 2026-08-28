import json
import mimetypes
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.core.files.storage import FileSystemStorage
from django.db.models import F
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_POST, require_http_methods

from economy import rewards

from .hls import MANIFEST_TYPE, manifest
from .media import MEDIA_CACHE, file_pk, hls_key, media_key
from .models import File, human_size
from .storage import file_storage
from .uploads import (
    MAX_DIRECT_SIZE, MAX_PARTS, abort_multipart, begin_multipart, check_name, direct_upload,
    finish_multipart, max_upload_size, multipart, part_size, part_urls, sign_upload, uploaded_parts,
)

# Внутренние адреса nginx: снаружи недоступны, попасть туда можно только
# заголовком X-Accel-Redirect (см. nginx.conf).
ACCEL_R2 = "/__r2"
ACCEL_LOCAL = "/__local/"

# Сколько браузеру держать кусок набора HLS.
#
# Сегмент неизменен по построению: в ключе uuid, перезапись в хранилище запрещена,
# а подпись входит в АДРЕС — сменится ключ подписи, сменится и адрес, то есть протухшим
# ответ оказаться не может в принципе. Поэтому `immutable`: без него браузер держит кусок
# в кеше, но на каждый переспрашивает «не изменилось?», и запрос всё равно доходит
# до нас. На пересмотре лекции это лишний круг на каждые 6 секунд видео.
SEGMENT_MAX_AGE = 365 * 24 * 3600
# Манифест — та же неизменная вещь, но это ВХОД в набор, и вечный кеш означал бы, что
# правку раздачи часть людей не увидит год. Час — и перепроверок почти нет, и руки
# не связаны.
MANIFEST_MAX_AGE = 3600


def _deliver(name):
    """Байты отдаёт nginx: он сам сходит в хранилище, закеширует и отправит файл.
    Наш процесс освобождается сразу — через приложение байты не идут никогда."""
    storage = file_storage()
    if not settings.MEDIA_ACCEL:
        return redirect(storage.url(name))  # разработка: nginx нет

    if isinstance(storage, FileSystemStorage):
        target = ACCEL_LOCAL + quote(name)
    else:
        # Путь и подпись берём из готовой ссылки: собирать её руками значит разойтись
        # с boto3 на первом же необычном имени файла.
        parts = urlsplit(storage.url(name))
        target = f"{ACCEL_R2}{parts.path}?{parts.query}"

    # Тип обязаны поставить мы: при X-Accel-Redirect заголовок приложения побеждает,
    # и с дефолтным text/html браузер показал бы pdf текстом, а картинку — ничем.
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return HttpResponse(content_type=content_type, headers={"X-Accel-Redirect": target})


@login_not_required
def download(request, token, name):
    """Через нас ссылка идёт ради счётчика; разрешение — сама подпись (см. media.file_url).
    Имя в хвосте адреса чисто для браузера, мы его не читаем."""
    pk = file_pk(token)
    if pk is None:
        raise Http404

    file = get_object_or_404(File, pk=pk)
    if "Range" not in request.headers:
        # Просмотрщик pdf дочитывает книгу кусками по тому же адресу — считаем только
        # первый запрос, иначе одно открытие давало бы десяток «скачиваний».
        File.objects.filter(pk=pk).update(downloads=F("downloads") + 1)
        # Пересчёт зовём не на каждое скачивание, а только когда набралась порция:
        # он стоит несколько запросов, а раздача файлов — самый горячий путь на сайте.
        # Счётчик читаем догрузочный, до update: разойтись он может разве что при
        # одновременных скачиваниях, а пропущенную порцию доберёт следующая.
        if file.uploader_id and (file.downloads + 1) % rewards.DOWNLOAD_SYNC_EVERY == 0:
            rewards.sync(file.uploader)
    return _deliver(file.file.name)


def _allow_our_origin(request, response):
    """Разрешить плееру прочитать этот ответ.

    Картинку и файл браузер берёт тегом, а `hls.js` — запросом из скрипта, и домен
    файлов для страницы чужой. Без этого заголовка ответ приходит, но отдать его
    плееру браузер отказывается, и видео молча не заводится.

    Список берём из `CSRF_TRUSTED_ORIGINS`: это ровно наши собственные адреса, и второй
    такой же список рано или поздно разошёлся бы с первым. В разработке домен файлов
    свой же, заголовка `Origin` нет, и ветка не срабатывает вовсе.
    """
    origin = request.headers.get("Origin")
    if origin and origin in settings.CSRF_TRUSTED_ORIGINS:
        response["Access-Control-Allow-Origin"] = origin
        # Range плеер шлёт при перемотке внутри сегмента; без разрешения браузер
        # отбросил бы такой запрос ещё до отправки.
        response["Access-Control-Allow-Headers"] = "Range"
        response["Access-Control-Expose-Headers"] = "Content-Length, Content-Range"
        patch_vary_headers(response, ["Origin"])
    return response


@login_not_required
@require_http_methods(["GET", "HEAD", "OPTIONS"])
def hls_piece(request, token, name):
    """Кусок HLS: манифест или сегмент. Разрешение — сама подпись, как и у файлов.

    Одна вьюха на оба, потому что и тем и другим плеер ходит по одинаковым адресам:
    манифест переписываем на лету, сегмент отдаёт nginx. Имя в хвосте адреса — ради
    расширения, читаем мы только подпись.
    """
    if request.method == "OPTIONS":  # предварительный запрос браузера перед Range
        return _allow_our_origin(request, HttpResponse(status=204))

    key = hls_key(token)
    if key is None:
        raise Http404

    if not key.endswith(".m3u8"):
        response = _deliver(key)
        # `immutable` годится только там, где наш адрес и есть ответ. В разработке
        # `_deliver` отдаёт РЕДИРЕКТ на подписанную ссылку хранилища, а та живёт сутки:
        # год кеша на такой ответ означает, что назавтра браузер идёт по протухшей
        # подписи и сегменты начинают отдавать 403 — без единого намёка на причину.
        # Ровно от этого бережётся и media_image ниже.
        response["Cache-Control"] = (
            f"public, max-age={SEGMENT_MAX_AGE}, immutable" if settings.MEDIA_ACCEL
            else f"private, max-age={MEDIA_CACHE}"
        )
        return _allow_our_origin(request, response)

    try:
        text = manifest(key)
    except FileNotFoundError:
        raise Http404
    response = HttpResponse(text, content_type=MANIFEST_TYPE)
    response["Cache-Control"] = f"public, max-age={MANIFEST_MAX_AGE}"
    return _allow_our_origin(request, response)


@login_not_required
def media_image(request, token):
    key = media_key(token)
    if key is None:
        raise Http404

    response = _deliver(key)
    if not settings.MEDIA_ACCEL:
        # Редирект живёт меньше подписи R2, иначе браузер переиспользовал бы протухшую.
        response["Cache-Control"] = f"private, max-age={MEDIA_CACHE}"
    return response


def _asked(request, *fields):
    """Тело запроса от Alpine или None, если прислали не то. Все ручки загрузки
    разговаривают одинаково — JSON туда, JSON обратно."""
    try:
        payload = json.loads(request.body)
    except ValueError:
        return None
    return payload if all(field in payload for field in fields) else None


def _named(payload):
    """Имя и размер из запроса, или None, если размер — не число.

    Тело приходит от скрипта в браузере, а скрипт бывает и чужой: без разбора здесь
    `int("сколько-то")` уронил бы ручку пятисоткой вместо внятного отказа.
    """
    try:
        return str(payload["name"])[:150], int(payload["size"] or 0)
    except (TypeError, ValueError):
        return None


def _refuse(request, name, size):
    """Причина, по которой такой файл принимать нельзя, или None."""
    if problem := check_name(name):
        return problem
    limit = max_upload_size(request.user)
    if size > limit:
        return f"«{name}» больше {human_size(limit)}"
    return None


@require_POST
def upload_url(request):
    """Подписанная ссылка, по которой браузер кладёт файл в R2 сам, минуя приложение."""
    if not direct_upload():
        return JsonResponse({"error": "Прямая загрузка недоступна"}, status=409)
    payload = _asked(request, "name", "size")
    asked = _named(payload) if payload else None
    if asked is None:
        return HttpResponseBadRequest("Ожидались name и size")
    name, size = asked

    if problem := _refuse(request, name, size):
        return JsonResponse({"error": problem}, status=400)
    if size > MAX_DIRECT_SIZE:
        return JsonResponse({"error": f"«{name}» не влезет одним куском"}, status=400)

    url, token = sign_upload(name)
    return JsonResponse({"url": url, "token": token})


def _stale():
    return JsonResponse({"error": "Загрузка устарела — начни заново"}, status=400)


@require_POST
def upload_start(request):
    """Начать многочастную загрузку — или продолжить прерванную.

    Что уже залито, спрашиваем у ХРАНИЛИЩА: браузер мог закрыться, потерять свою память
    или соврать, а правду знает только бакет.
    """
    if not direct_upload():
        return JsonResponse({"error": "Прямая загрузка недоступна"}, status=409)
    payload = _asked(request, "name", "size")
    asked = _named(payload) if payload else None
    if asked is None:
        return HttpResponseBadRequest("Ожидались name и size")
    name, size = asked

    if problem := _refuse(request, name, size):
        return JsonResponse({"error": problem}, status=400)

    # Продолжаем, только если присланный токен наш И такая загрузка в хранилище жива.
    # Иначе начинаем заново: отдать браузеру мёртвый номер — значит дать ему залить
    # гигабайты в никуда и узнать об этом на самом последнем шаге.
    started = multipart(payload.get("resume") or "")
    done = uploaded_parts(started) if started else None
    token = payload["resume"] if done is not None else begin_multipart(name)
    return JsonResponse({"token": token, "partSize": part_size(size), "done": done or {}})


@require_POST
def upload_parts(request):
    """Ссылки на очередную порцию частей."""
    payload = _asked(request, "token", "numbers")
    if payload is None:
        return HttpResponseBadRequest("Ожидались token и numbers")
    # Токен разбираем сами: ключ и номер загрузки приходят от браузера, и без подписи
    # он мог бы дописаться в чужую.
    started = multipart(payload["token"])
    if started is None:
        return _stale()
    try:
        numbers = [int(number) for number in payload["numbers"]][:MAX_PARTS]
    except (TypeError, ValueError):
        return HttpResponseBadRequest("numbers — это список номеров частей")
    return JsonResponse({"urls": part_urls(started, numbers)})


@require_POST
def upload_finish(request):
    """Собрать объект из частей и вернуть токен, который форма пришлёт вместо файла."""
    payload = _asked(request, "token", "parts")
    if payload is None:
        return HttpResponseBadRequest("Ожидались token и parts")
    started = multipart(payload["token"])
    if started is None:
        return _stale()
    try:
        parts = {int(number): str(tag) for number, tag in payload["parts"].items()}
    except (AttributeError, TypeError, ValueError):
        return HttpResponseBadRequest("parts — это {номер части: etag}")
    if not parts:
        return JsonResponse({"error": "Нечего собирать: ни одной части"}, status=400)
    return JsonResponse({"token": finish_multipart(started, parts)})


@require_POST
def upload_abort(request):
    """Бросить начатое. Незаконченные части занимают место в бакете и стоят денег."""
    payload = _asked(request, "token")
    started = multipart(payload["token"]) if payload else None
    if started is not None:
        abort_multipart(started)
    return JsonResponse({"ok": True})
