import json
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.urls import reverse

from .models import File, human_size
from .storage import file_storage

# Через приложение файл держит воркер и место на диске — лимит скромный.
MAX_FILE_SIZE = 200 * 1024 * 1024
# Прямо в R2 упирается только в потолок одиночного PUT (около 5 ГБ);
# TODO: больше — только multipart, подписывать каждую часть отдельно.
MAX_DIRECT_SIZE = 4 * 1024 * 1024 * 1024
# Медиа отдаётся с домена сайта: html/svg/js в браузере выполнились бы как код сайта.
# Второй рубеж — Content-Disposition: attachment на /media/ в nginx.
FORBIDDEN_EXTENSIONS = {"html", "htm", "xhtml", "svg", "js", "mjs", "exe", "msi", "bat", "cmd", "sh"}
UPLOAD_SALT = "attachments.direct-upload"
UPLOAD_MAX_AGE = 6 * 3600  # столько живёт токен между подписью и отправкой формы


def direct_upload():
    """Прямая загрузка возможна, только когда файлы лежат в R2."""
    return bool(settings.R2_BUCKET)


def max_upload_size():
    return MAX_DIRECT_SIZE if direct_upload() else MAX_FILE_SIZE


def upload_limits():
    """Настройки для Alpine-компонента fileForm: правило одно и живёт здесь."""
    return json.dumps({
        "maxSize": max_upload_size(),
        "forbidden": sorted(FORBIDDEN_EXTENSIONS),
        "direct": direct_upload(),
        "signUrl": reverse("upload_url"),
    })


def saved_files(owner):
    """Уже сохранённые файлы для Alpine: порядок меняется перетаскиванием, поэтому
    строки рисует компонент, а не шаблонный цикл."""
    files = owner.files.all() if owner else []
    return json.dumps(
        [{"pk": f.pk, "name": f.name, "extension": f.extension.upper(), "size": f.human_size()} for f in files],
        ensure_ascii=False,
    )


def check_name(name):
    """Причина отказа по имени файла или None."""
    if Path(name).suffix.lstrip(".").lower() in FORBIDDEN_EXTENSIONS:
        return f"«{name}» — такой тип файла загружать нельзя"
    return None


def check_uploads(uploads):
    """Ошибки по файлам, пришедшим ЧЕРЕЗ приложение, списком."""
    errors = []
    for upload in uploads:
        if upload.size > MAX_FILE_SIZE:
            errors.append(f"«{upload.name}» больше {human_size(MAX_FILE_SIZE)}")
        elif problem := check_name(upload.name):
            errors.append(problem)
    return errors


def sign_upload(name):
    """Ключ выбирает сервер и подтверждает подписью: иначе к своей книге можно было бы
    прицепить любой чужой объект из бакета, просто прислав его ключ."""
    key = f"uploads/{uuid4().hex}/{Path(name).name}"
    storage = file_storage()
    prefix = f"{storage.location}/" if getattr(storage, "location", "") else ""
    url = storage.connection.meta.client.generate_presigned_url(
        "put_object",
        Params={"Bucket": storage.bucket_name, "Key": prefix + key},
        ExpiresIn=UPLOAD_MAX_AGE,
    )
    return url, signing.dumps({"key": key, "name": name}, salt=UPLOAD_SALT)


def pending_uploads(request):
    """Файлы, которые уже уехали в хранилище, но не доехали до сохранения — форма
    вернулась с ошибкой. Отдаём токены обратно в разметку, чтобы не заливать заново."""
    pending = []
    for token in request.POST.getlist("uploaded"):
        try:
            payload = signing.loads(token, salt=UPLOAD_SALT, max_age=UPLOAD_MAX_AGE)
        except signing.BadSignature:
            continue
        pending.append({"token": token, "name": payload["name"]})
    return pending


def _adopt(token, owner, request, order, name=""):
    """Файл уже в хранилище — заводим на него запись, проверив, что он реально доехал."""
    try:
        payload = signing.loads(token, salt=UPLOAD_SALT, max_age=UPLOAD_MAX_AGE)
    except signing.BadSignature:
        return None

    storage = file_storage()
    try:
        size = storage.size(payload["key"])
    except FileNotFoundError:
        return None  # браузер не догрузил или соврал

    if size > MAX_DIRECT_SIZE:
        storage.delete(payload["key"])  # подписью размер не ограничить, ловим после факта
        return None

    return File.objects.create(
        **{owner._meta.model_name: owner},
        name=(name or payload["name"])[:150], file=payload["key"], size=size,
        uploader=request.user, order=order,
    )


def sync_files(request, owner):
    """Переименование и удаление существующих файлов + приём новых.
    Имя модели-владельца совпадает с именем FK в File (book/material) — по нему и привязываем."""
    for file in owner.files.all():
        if request.POST.get(f"delete-{file.pk}"):
            file.delete()  # post_delete снесёт и сам блоб
            continue
        name = request.POST.get(f"name-{file.pk}", "").strip()
        if name and name != file.name:
            file.name = name[:150]
            file.save(update_fields=["name"])

    # Порядок строк на экране: перетаскивание переставляет скрытые input name="order".
    by_pk = {file.pk: file for file in owner.files.all()}
    for index, pk in enumerate(request.POST.getlist("order")):
        file = by_pk.get(int(pk)) if pk.isdigit() else None
        if file and file.order != index:
            file.order = index
            file.save(update_fields=["order"])

    # Имена новых файлов идут отдельными списками, но строго парами со своим файлом:
    # они рисуются в той же строке формы, поэтому порядок совпадает.
    order = len(by_pk)  # новые файлы встают в конец
    names = request.POST.getlist("files-name")
    for index, upload in enumerate(request.FILES.getlist("files")):
        chosen = names[index].strip() if index < len(names) else ""
        File.objects.create(
            **{owner._meta.model_name: owner},
            name=(chosen or upload.name)[:150], file=upload, uploader=request.user, order=order,
        )
        order += 1

    names = request.POST.getlist("uploaded-name")
    for index, token in enumerate(request.POST.getlist("uploaded")):
        chosen = names[index].strip() if index < len(names) else ""
        if _adopt(token, owner, request, order, name=chosen):
            order += 1
