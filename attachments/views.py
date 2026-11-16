import json
import logging
from functools import wraps
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.db.models import F
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_POST, require_http_methods

from core.throttle import client_ip, throttled
from economy import rewards

from .hls import MANIFEST_TYPE, manifest
from .media import MEDIA_CACHE, file_pk, hls_key, media_key
from .models import File, human_size
from .storage import content_type, file_storage
from .uploads import (
    MAX_DIRECT_SIZE, MAX_PARTS, QUOTA_DAYS, SIGNS_PER_HOUR, WEEKLY_QUOTA, Oversized, abort_multipart, begin_multipart,
    check_name, direct_upload, finish_multipart, max_upload_size, multipart, part_size, part_urls, parts_for,
    quota_left, sign_upload, uploaded_parts,
)

logger = logging.getLogger(__name__)

# Внутренние адреса nginx: снаружи недоступны, попасть туда можно только
# заголовком X-Accel-Redirect (см. nginx.conf).
ACCEL_R2 = "/__r2"
ACCEL_LOCAL = "/__local/"

# Кеш куска HLS. Сегмент неизменен (uuid, без перезаписи, подпись в адресе) — `immutable`,
# иначе браузер переспрашивает «не изменилось?» на каждый сегмент.
SEGMENT_MAX_AGE = 365 * 24 * 3600
# Манифест — вход в набор: вечный кеш спрятал бы правку раздачи на год. Час.
MANIFEST_MAX_AGE = 3600
# Столько одно скачивание файла с одного адреса не считается повторно.
DOWNLOAD_ONCE = 24 * 3600


def files_host_only(view):
    """Загруженное людьми — только с домена файлов: иначе чужой файл в одном origin с куками
    сессии. Переадресация временная: постоянную браузер помнил бы и после отключения домена."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        base = settings.FILES_BASE_URL
        if base and request.get_host().lower() != urlsplit(base).netloc.lower():
            return redirect(base + request.get_full_path())
        return view(request, *args, **kwargs)
    return wrapped


def _deliver(name):
    """Байты отдаёт nginx (X-Accel-Redirect): он сходит в хранилище и закеширует."""
    storage = file_storage()
    if not settings.MEDIA_ACCEL:
        return redirect(storage.url(name))  # разработка: nginx нет

    if isinstance(storage, FileSystemStorage):
        target = ACCEL_LOCAL + quote(name)
    else:
        # Путь и подпись — из готовой ссылки boto3: руками разошлись бы на необычном имени.
        parts = urlsplit(storage.url(name))
        target = f"{ACCEL_R2}{parts.path}?{parts.query}"

    # Тип ставим и мы: с text/html по умолчанию pdf показался бы текстом. Но с R2 побеждает тип
    # из объекта — поэтому список один (storage.content_type), и заливка объявляет тот же.
    return HttpResponse(content_type=content_type(name), headers={"X-Accel-Redirect": target})


def _first_today(request, pk):
    """Первое ли скачивание файла с этого адреса за сутки: ссылка открыта без входа, а
    счётчик приносит токены. Кто скачал, не знаем — только адрес. Кэш лёг — не считаем."""
    try:
        return cache.add(f"download:{pk}:{client_ip(request)}", 1, DOWNLOAD_ONCE)
    except Exception:  # noqa: BLE001 — какой именно, зависит от бекенда кэша
        logger.exception("Кэш недоступен, скачивание файла %s не посчитано", pk)
        return False


@login_not_required
@files_host_only
def download(request, token, name):
    """Через нас — ради счётчика; разрешение — подпись (media.file_url). Имя в хвосте не читаем."""
    pk = file_pk(token)
    if pk is None:
        raise Http404

    file = get_object_or_404(File, pk=pk)
    # Считаем только первый запрос (pdf дочитывают кусками по Range), и не <img>.
    counted = "Range" not in request.headers and request.headers.get("Sec-Fetch-Dest") != "image"
    if counted and _first_today(request, pk):
        File.objects.filter(pk=pk).update(downloads=F("downloads") + 1)
        # Пересчёт — раз в порцию скачиваний: раздача — самый горячий путь. Пропущенную
        # порцию доберёт следующая.
        if file.uploader_id and (file.downloads + 1) % rewards.DOWNLOAD_SYNC_EVERY == 0:
            rewards.sync(file.uploader)
    return _deliver(file.file.name)


def _allow_our_origin(request, response):
    """Разрешить плееру прочитать ответ: hls.js берёт куски запросом, домен файлов для
    страницы чужой. Список — CSRF_TRUSTED_ORIGINS. В dev Origin нет, ветка не срабатывает."""
    origin = request.headers.get("Origin")
    if origin and origin in settings.CSRF_TRUSTED_ORIGINS:
        response["Access-Control-Allow-Origin"] = origin
        # Range плеер шлёт при перемотке; без разрешения браузер отбросил бы запрос.
        response["Access-Control-Allow-Headers"] = "Range"
        response["Access-Control-Expose-Headers"] = "Content-Length, Content-Range"
        patch_vary_headers(response, ["Origin"])
    return response


@login_not_required
@require_http_methods(["GET", "HEAD", "OPTIONS"])
@files_host_only
def hls_piece(request, token, name):
    """Кусок HLS: манифест переписываем на лету, сегмент отдаёт nginx. Разрешение — подпись."""
    if request.method == "OPTIONS":  # предварительный запрос браузера перед Range
        return _allow_our_origin(request, HttpResponse(status=204))

    key = hls_key(token)
    if key is None:
        raise Http404

    if not key.endswith(".m3u8"):
        response = _deliver(key)
        # `immutable` — только под nginx: в dev ответ — редирект на подпись хранилища,
        # она живёт сутки.
        response["Cache-Control"] = (
            f"public, max-age={SEGMENT_MAX_AGE}, immutable" if settings.MEDIA_ACCEL
            else f"private, max-age={MEDIA_CACHE}"
        )
        # Под nginx разрешение ставит nginx: два разрешения браузер отвергает.
        # В dev ответ наш, и заголовок тоже (nginx-files.conf).
        return response if settings.MEDIA_ACCEL else _allow_our_origin(request, response)

    try:
        text = manifest(key)
    except FileNotFoundError:
        raise Http404
    response = HttpResponse(text, content_type=MANIFEST_TYPE)
    response["Cache-Control"] = f"public, max-age={MANIFEST_MAX_AGE}"
    return _allow_our_origin(request, response)


@login_not_required
@files_host_only
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
    # `null` или `5` — тоже JSON, но проверка полей на них падала бы TypeError, то есть пятисоткой.
    return payload if isinstance(payload, dict) and all(field in payload for field in fields) else None


def _named(payload):
    """Имя и размер из запроса, или None, если размер — не число (скрипт бывает и чужой)."""
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
    left = quota_left(request.user)
    if size > left:
        return (f"«{name}» не влезает в недельную квоту: осталось {human_size(left)} "
                f"из {human_size(WEEKLY_QUOTA)}. Место освобождается через {QUOTA_DAYS} дней после загрузки")
    return None


def _too_often(request):
    """Ответ-отказ, если подписей за час слишком много. Ссылка на загрузку — это место
    в платном бакете, а ночная уборка снимает брошенное только через сутки."""
    if throttled(f"upload:sign:{request.user.pk}", SIGNS_PER_HOUR):
        return JsonResponse({"error": "Слишком много загрузок за час — попробуй позже"}, status=429)
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
    if refusal := _too_often(request):
        return refusal
    if size > MAX_DIRECT_SIZE:
        return JsonResponse({"error": f"«{name}» не влезет одним куском"}, status=400)

    url, token, kind = sign_upload(name)
    return JsonResponse({"url": url, "token": token, "type": kind})


def _stale():
    return JsonResponse({"error": "Загрузка устарела — начни заново"}, status=400)


@require_POST
def upload_start(request):
    """Начать многочастную загрузку или продолжить прерванную; что залито — спрашиваем у хранилища."""
    if not direct_upload():
        return JsonResponse({"error": "Прямая загрузка недоступна"}, status=409)
    payload = _asked(request, "name", "size")
    asked = _named(payload) if payload else None
    if asked is None:
        return HttpResponseBadRequest("Ожидались name и size")
    name, size = asked

    if problem := _refuse(request, name, size):
        return JsonResponse({"error": problem}, status=400)
    if refusal := _too_often(request):
        return refusal

    # Продолжаем, только если токен наш И загрузка в хранилище жива; иначе — заново.
    started = multipart(payload.get("resume") or "")
    done = uploaded_parts(started) if started else None
    token = payload["resume"] if done is not None else begin_multipart(name, size)
    return JsonResponse({"token": token, "partSize": part_size(size), "done": done or {}})


@require_POST
def upload_parts(request):
    """Ссылки на очередную порцию частей."""
    payload = _asked(request, "token", "numbers")
    if payload is None:
        return HttpResponseBadRequest("Ожидались token и numbers")
    # Токен подписан: ключ и номер приходят от браузера.
    started = multipart(payload["token"])
    if started is None:
        return _stale()
    try:
        numbers = [int(number) for number in payload["numbers"]][:MAX_PARTS]
    except (TypeError, ValueError):
        return HttpResponseBadRequest("numbers — это список номеров частей")
    # Не больше частей, чем нужно объявленному размеру.
    last = parts_for(started)
    return JsonResponse({"urls": part_urls(started, [number for number in numbers if 1 <= number <= last])})


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
    try:
        token = finish_multipart(started, parts, limit=max_upload_size(request.user))
    except Oversized:
        return JsonResponse({"error": f"«{started['name']}» больше, чем было объявлено при начале загрузки"}, status=400)
    return JsonResponse({"token": token})


@require_POST
def upload_abort(request):
    """Бросить начатое. Незаконченные части занимают место в бакете и стоят денег."""
    payload = _asked(request, "token")
    started = multipart(payload["token"]) if payload else None
    if started is not None:
        abort_multipart(started)
    return JsonResponse({"ok": True})
