from django.db.models import F
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect

from .models import File


def download(request, pk):
    """Ссылка идёт через нас только ради счётчика — сам файл отдаёт хранилище.
    Работает одинаково для локального диска и для R2, меняется лишь url."""
    file = get_object_or_404(File.objects.select_related("book", "material"), pk=pk)
    owner = file.book or file.material
    # TODO: пускать модераторов — вместе с правами moderate_book / moderate_material
    if owner and not owner.approved and owner.uploader_id != request.user.pk:
        raise Http404

    File.objects.filter(pk=pk).update(downloads=F("downloads") + 1)
    return redirect(file.file.url)
