import json
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.core.files.storage import FileSystemStorage
from django.db.models import F
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .media import MEDIA_CACHE, file_pk, media_key
from .models import File, human_size
from .storage import file_storage
from .uploads import MAX_DIRECT_SIZE, check_name, direct_upload, sign_upload

# Внутренние адреса nginx: снаружи недоступны, попасть туда можно только
# заголовком X-Accel-Redirect (см. nginx.conf).
ACCEL_R2 = "/__r2"
ACCEL_LOCAL = "/__local/"


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
    return HttpResponse(headers={"X-Accel-Redirect": target})


@login_not_required
def download(request, token, name):
    """Через нас ссылка идёт ради счётчика; разрешение — сама подпись (см. media.file_url).
    Имя в хвосте адреса чисто для браузера, мы его не читаем."""
    pk = file_pk(token)
    if pk is None:
        raise Http404

    file = get_object_or_404(File, pk=pk)
    File.objects.filter(pk=pk).update(downloads=F("downloads") + 1)
    return _deliver(file.file.name)


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


@require_POST
def upload_url(request):
    """Подписанная ссылка, по которой браузер кладёт файл в R2 сам, минуя приложение."""
    if not direct_upload():
        return JsonResponse({"error": "Прямая загрузка недоступна"}, status=409)

    try:
        payload = json.loads(request.body)
        name = str(payload["name"])[:150]
        size = int(payload["size"])
    except (ValueError, TypeError, KeyError):
        return HttpResponseBadRequest("Ожидались name и size")

    if problem := check_name(name):
        return JsonResponse({"error": problem}, status=400)
    if size > MAX_DIRECT_SIZE:
        return JsonResponse({"error": f"«{name}» больше {human_size(MAX_DIRECT_SIZE)}"}, status=400)

    url, token = sign_upload(name)
    return JsonResponse({"url": url, "token": token})
