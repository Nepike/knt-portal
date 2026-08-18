import json
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.urls import reverse

from .media import media_url
from .models import File, Image, human_size
from .storage import file_storage

# Через приложение файл держит воркер и место на диске — лимит скромный.
MAX_FILE_SIZE = 200 * 1024 * 1024
# Прямо в R2 упирается только в потолок одиночного PUT (около 5 ГБ);
# TODO: больше — только multipart, подписывать каждую часть отдельно.
MAX_DIRECT_SIZE = 4 * 1024 * 1024 * 1024
# Картинки галереи идут обычным multipart и держат воркер — им хватит и десятки.
MAX_IMAGE_SIZE = 10 * 1024 * 1024
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


def saved_images(owner):
    """Уже сохранённые картинки для Alpine-компонента gallery."""
    images = owner.images.all() if owner else []
    return json.dumps(
        [{"pk": i.pk, "url": media_url(i.image), "name": i.name} for i in images], ensure_ascii=False,
    )


def check_images(uploads):
    """Ошибки по картинкам галереи. Что это вообще картинка, проверит ImageField."""
    return [
        f"«{upload.name}» больше {human_size(MAX_IMAGE_SIZE)}"
        for upload in uploads if upload.size > MAX_IMAGE_SIZE
    ]


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
    прицепить любой чужой объект из бакета, просто прислав его ключ.

    ВАЖНО: по этой ссылке браузер кладёт файл НАПРЯМУЮ в R2, а это другой домен — значит
    в самом бакете должен быть разрешён CORS с домена сайта, иначе браузер не отправит
    даже предзапрос. В коде это не настраивается, только в панели Cloudflare:

        AllowedOrigins  https://knt-mipt.ru  (+ http://127.0.0.1:8000 и localhost для dev)
        AllowedMethods  PUT
        AllowedHeaders  *

    Симптом забытого домена — «Не удалось загрузить …» в форме и 403 на OPTIONS: подпись
    и права тут ни при чём, отказывает браузер. На это наступили при переезде с test.
    """
    key = f"uploads/{uuid4().hex}/{Path(name).name}"
    storage = file_storage()
    prefix = f"{storage.location}/" if getattr(storage, "location", "") else ""
    url = storage.connection.meta.client.generate_presigned_url(
        "put_object",
        Params={"Bucket": storage.bucket_name, "Key": prefix + key},
        ExpiresIn=UPLOAD_MAX_AGE,
    )
    return url, signing.dumps({"key": key, "name": name}, salt=UPLOAD_SALT)


def _unsign(token):
    """Содержимое токена или None, если подпись не наша или протухла."""
    try:
        return signing.loads(token, salt=UPLOAD_SALT, max_age=UPLOAD_MAX_AGE)
    except signing.BadSignature:
        return None


def _stored_size(request, key):
    """Размер объекта в хранилище (None — объекта нет). Кешируем на запрос: об одном
    и том же файле спрашивают и check_pending до сохранения, и _adopt после."""
    cache = getattr(request, "_upload_sizes", None)
    if cache is None:
        cache = request._upload_sizes = {}
    if key not in cache:
        try:
            cache[key] = file_storage().size(key)
        except FileNotFoundError:
            cache[key] = None
    return cache[key]


def check_pending(request):
    """Каждый присланный токен должен указывать на реально лежащий в хранилище файл.
    Без этой проверки недоехавший файл терялся бы МОЛЧА: книга сохранялась бы без него,
    а человек узнавал бы об этом, только открыв её."""
    errors = []
    for token in request.POST.getlist("uploaded"):
        payload = _unsign(token)
        if payload is None:
            errors.append("Один из файлов не доехал до хранилища — загрузи его заново.")
            continue
        size = _stored_size(request, payload["key"])
        if size is None:
            errors.append(f"«{payload['name']}» не доехал до хранилища — загрузи заново.")
        elif size > MAX_DIRECT_SIZE:
            errors.append(f"«{payload['name']}» больше {human_size(MAX_DIRECT_SIZE)}")
    return errors


def pending_uploads(request):
    """Файлы, которые уже уехали в хранилище, но не доехали до сохранения — форма
    вернулась с ошибкой. Отдаём токены обратно в разметку, чтобы не заливать заново."""
    unsigned = ((token, _unsign(token)) for token in request.POST.getlist("uploaded"))
    return [{"token": token, "name": payload["name"]} for token, payload in unsigned if payload]


def _adopt(token, owner, request, order, name=""):
    """Файл уже в хранилище — заводим на него запись. Что он туда доехал и влезает
    в лимит, проверил check_pending: сюда доходят только целые загрузки."""
    payload = _unsign(token)
    size = _stored_size(request, payload["key"]) if payload else None
    if size is None:
        return None

    return File.objects.create(
        **{owner._meta.model_name: owner},
        name=(name or payload["name"])[:150], file=payload["key"], size=size,
        uploader=request.user, order=order,
    )


def sync_images(request, owner):
    """Галерея: удаление помеченных, порядок и приём новых.

    Проще файлов: картинки маленькие, поэтому идут обычным multipart через приложение,
    без подписанных ссылок и прогресса. Поле — images, порядок — image-order.
    """
    for image in owner.images.all():
        if request.POST.get(f"delete-image-{image.pk}"):
            image.delete()  # post_delete снесёт и сам блоб

    by_pk = {image.pk: image for image in owner.images.all()}
    for index, pk in enumerate(request.POST.getlist("image-order")):
        image = by_pk.get(int(pk)) if pk.isdigit() else None
        if image and image.order != index:
            image.order = index
            image.save(update_fields=["order"])

    order = len(by_pk)
    for upload in request.FILES.getlist("images"):
        Image.objects.create(
            **{owner._meta.model_name: owner},
            name=upload.name[:150], image=upload, uploader=request.user, order=order,
        )
        order += 1


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
    order = len(by_pk)
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
