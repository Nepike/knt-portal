import json

from django.db.models import F
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .models import File, human_size
from .uploads import MAX_DIRECT_SIZE, check_name, direct_upload, sign_upload


def download(request, pk):
    """Ссылка идёт через нас только ради счётчика — сам файл отдаёт хранилище.
    Работает одинаково для локального диска и для R2, меняется лишь url."""
    file = get_object_or_404(File.objects.select_related("book", "material"), pk=pk)
    owner = file.book or file.material
    if owner and not owner.approved and owner.uploader_id != request.user.pk:
        # Право берём по владельцу: library.change_book или materials.change_material.
        if not request.user.has_perm(f"{owner._meta.app_label}.change_{owner._meta.model_name}"):
            raise Http404

    File.objects.filter(pk=pk).update(downloads=F("downloads") + 1)
    return redirect(file.file.url)


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
