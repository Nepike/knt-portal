import json
import math
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import ClientError
from django.conf import settings
from django.core import signing
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image as PilImage

from .media import media_url
from .models import File, Image, human_size
from .scrub import Unreadable, scrub
from .storage import content_type, file_storage, random_key

MB = 1024 * 1024
# Через приложение файл держит воркер и место на диске — лимит скромный.
MAX_FILE_SIZE = 200 * 1024 * 1024
# Одним PUT — до этого. Дальше многочастная загрузка (см. ниже).
MAX_DIRECT_SIZE = 4 * 1024 * 1024 * 1024
# Сырьё лекции: два часа записи — около 16 ГБ (docs/media-pipeline.md). Потолок — только
# с правом на лекторий: место в бакете стоит денег.
MAX_LECTURE_SIZE = 32 * 1024 * 1024 * 1024

# Больше — частями: после обрыва докладываются только недостающие.
MULTIPART_FROM = 64 * 1024 * 1024
# Часть. У S3 минимум 5 МБ (кроме последней) и не больше 10 000 частей на объект.
PART_SIZE = 16 * 1024 * 1024
MAX_PARTS = 10000
MULTIPART_SALT = "attachments.multipart"
# Столько живёт начатая загрузка: к ней возвращаются и назавтра.
MULTIPART_MAX_AGE = 24 * 3600
# Картинки галереи идут обычным multipart и держат воркер — им хватит и десятки.
MAX_IMAGE_SIZE = 10 * 1024 * 1024
# Точек в картинке для других: зритель разжимает её у себя (PNG в 17 КБ — 144 Мп).
# Тот же предел — общий потолок Pillow (AttachmentsConfig.ready).
MAX_PICTURE_PIXELS = 50_000_000
# Что браузер исполнил бы, а человек запустил. Рубеж не главный: тип всё равно назначает
# закрытый storage.CONTENT_TYPES.
FORBIDDEN_EXTENSIONS = {"html", "htm", "xhtml", "svg", "js", "mjs", "exe", "msi", "bat", "cmd", "sh"}
UPLOAD_SALT = "attachments.direct-upload"
# Папка прямых загрузок. Токен годится только своей папке: иначе файл печати, прицепленный
# к материалу, исчез бы с уборкой печати.
PREFIX = "uploads"
UPLOAD_MAX_AGE = 6 * 3600  # столько живёт токен между подписью и отправкой формы
# Столько живёт ссылка на запись: подпись S3 многоразовая, по ней можно перезалить уже
# принятый файл. Одним PUT идёт только меньшее MULTIPART_FROM — часа хватает.
PUT_MAX_AGE = 3600
# Потолок файла материала и книги при прямой загрузке: бакет платный.
MAX_MATERIAL_SIZE = 500 * 1024 * 1024
# Недельная квота на человека.
WEEKLY_QUOTA = 5 * 1024 * 1024 * 1024
QUOTA_DAYS = 7
SIGNS_PER_HOUR = 60  # подписей ссылок на загрузку в час на человека


def direct_upload():
    return bool(settings.R2_BUCKET)


def _lecturer(user):
    """Сдаёт записи лекций. Право строкой: вложения не зависят от лектория."""
    return user is not None and user.has_perm("lectorium.add_playlist")


def max_upload_size(user=None):
    """Потолок одного файла для этого человека."""
    if not direct_upload():
        return MAX_FILE_SIZE
    return MAX_LECTURE_SIZE if _lecturer(user) else MAX_MATERIAL_SIZE


def quota_left(user):
    """Сколько ещё можно залить на этой неделе, байт. У лектория квоты нет."""
    if _lecturer(user):
        return math.inf
    since = timezone.now() - timedelta(days=QUOTA_DAYS)
    spent = File.objects.filter(uploader=user, created__gte=since).aggregate(total=Sum("size"))["total"] or 0
    return max(WEEKLY_QUOTA - spent, 0)


def parts_for(payload):
    """Сколько частей нужно загрузке. Токен без размера (старый) — сколько попросят."""
    size = payload.get("size")
    return -(-size // part_size(size)) if size else MAX_PARTS


def upload_limits(user=None):
    """Настройки для Alpine-компонента fileForm: правило одно и живёт здесь."""
    return json.dumps({
        "maxSize": max_upload_size(user),
        "forbidden": sorted(FORBIDDEN_EXTENSIONS),
        "direct": direct_upload(),
        "signUrl": reverse("upload_url"),
        # Многочастная загрузка: с какого размера и куда за частями.
        "partsFrom": MULTIPART_FROM,
        "startUrl": reverse("upload_start"),
        "partsUrl": reverse("upload_parts"),
        "finishUrl": reverse("upload_finish"),
        "abortUrl": reverse("upload_abort"),
    })


def saved_files(owner):
    """Сохранённые файлы для Alpine: строки рисует компонент (порядок меняют перетаскиванием)."""
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


# Картинки, которые покажет любой браузер: формат Pillow → расширение ключа. MPO — тот же JPEG.
PICTURES = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
PICTURE_HINT = "Подойдут JPEG, PNG, WebP и GIF"
# Для полей формы: файл, который Pillow не открыл (HEIC), Django отсекает раньше нашей проверки.
UNREADABLE_PICTURE = f"Не получилось прочитать картинку. {PICTURE_HINT}; снимок HEIC с айфона сохрани как JPEG."
# Начало HEIC (фото айфона) — свой отказ: «это не картинка» про свой снимок запутает.
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}


def inspect_picture(upload):
    """(формат, сколько точек) по СОДЕРЖИМОМУ; формат None — не картинка. Имя и тип из
    запроса пишет отправитель. Пиксели не разжимаются; картинка, которую Pillow не открыл
    из-за размера, — «слишком большая»: точек бесконечно много.
    """
    try:
        with PilImage.open(upload) as image:
            kind, pixels = image.format, image.width * image.height
            image.verify()
    except PilImage.DecompressionBombError:
        return None, math.inf
    except Exception:  # noqa: BLE001 — Pillow бросает чем придётся, и всё это «не картинка»
        return None, 0
    finally:
        upload.seek(0)  # файл ещё сохранять
    return kind, pixels


def picture_format(upload):
    """Формат картинки по содержимому (как его зовёт Pillow) или None."""
    return inspect_picture(upload)[0]


def too_many_pixels(name):
    return (f"«{name}» больше {MAX_PICTURE_PIXELS // 1_000_000} мегапикселей. "
            "Уменьши картинку или сохрани её в меньшем разрешении")


def check_picture(upload):
    """Причина не принимать файл как картинку, или None. Берём только то, что покажет
    любой браузер: HEIC умеет один Safari."""
    kind, pixels = inspect_picture(upload)
    if pixels > MAX_PICTURE_PIXELS:
        return too_many_pixels(upload.name)
    if kind in PICTURES:
        # Сохраняется только очищенной: не вышло очистить — не берём.
        try:
            scrub(upload.read(), kind)
        except Unreadable:
            return f"«{upload.name}» — {UNREADABLE_PICTURE[0].lower()}{UNREADABLE_PICTURE[1:]}"
        finally:
            upload.seek(0)
        return None
    head = upload.read(12)
    upload.seek(0)
    if head[4:8] == b"ftyp" and head[8:12] in HEIC_BRANDS:
        return f"«{upload.name}» — формат HEIC браузеры не показывают. Сохрани как JPEG и загрузи снова"
    if kind is None:
        return f"«{upload.name}» — это не картинка. {PICTURE_HINT}"
    return f"«{upload.name}» — формат {kind} показывают не все браузеры. {PICTURE_HINT}"


def scrubbed(upload):
    """Картинка для других: без метаданных (attachments/scrub.py) и под безликим именем —
    ключ читается из адреса (подпись сжата, не зашифрована). Расширение — по содержимому:
    по нему назначается тип. Зовётся после check_picture.
    """
    kind = picture_format(upload)
    upload.seek(0)
    name = "image" + PICTURES[kind]
    clean = SimpleUploadedFile(name, scrub(upload.read(), kind), content_type=content_type(name))
    upload.seek(0)
    return clean


def check_images(uploads):
    """Ошибки по картинкам галереи: размер и формат."""
    errors = []
    for upload in uploads:
        if upload.size > MAX_IMAGE_SIZE:
            errors.append(f"«{upload.name}» больше {human_size(MAX_IMAGE_SIZE)}")
        elif problem := check_picture(upload):
            errors.append(problem)
    return errors


def drop_replaced(form, field="image"):
    """Снять из хранилища картинку, которую форма заменила или убрала: запись жива, и
    post_delete её не снимет. Звать ПОСЛЕ save()."""
    was = form.initial.get(field)
    if was and str(was) != str(getattr(form.instance, field)):
        was.storage.delete(str(was))


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


def sign_upload(name, folder=PREFIX):
    """Подписанная ссылка на прямую загрузку в R2. Ключ выбирает сервер: иначе к своей книге
    можно было бы прицепить чужой объект. `folder` — своя папка печати (`printing/`).

    Тип входит в подпись и возвращается браузеру: он обязан прислать ровно его, иначе тип
    объекта выбирал бы загружающий.

    CORS бакета настраивается в панели Cloudflare, не в коде:

        AllowedOrigins  https://knt-mipt.ru  (+ http://127.0.0.1:8000 и localhost для dev)
        AllowedMethods  PUT
        AllowedHeaders  *
        ExposeHeaders   ETag

    Забытый домен — «Не удалось загрузить …» и 403 на OPTIONS. ExposeHeaders нужен
    многочастной: без него скрипт не прочтёт ETag части.
    """
    key = new_key(name, folder)
    kind = content_type(key)
    storage = file_storage()
    url = storage.connection.meta.client.generate_presigned_url(
        "put_object",
        Params={"Bucket": storage.bucket_name, "Key": _prefixed(storage, key), "ContentType": kind},
        ExpiresIn=PUT_MAX_AGE,
    )
    return url, adopt_token(key, name), kind


def new_key(name, folder=PREFIX):
    """Ключ прямой загрузки — через `random_key`: ключ обязан влезть в 100 символов
    `File.file`, а имя от браузера — до 150."""
    return random_key(folder, name)


def sign_download(key):
    """Подписанная ссылка на ЧТЕНИЕ: по ней пекарня берёт сырьё прямо из R2, мимо нас."""
    storage = file_storage()
    return storage.connection.meta.client.generate_presigned_url(
        "get_object",
        Params={"Bucket": storage.bucket_name, "Key": _prefixed(storage, key)},
        ExpiresIn=UPLOAD_MAX_AGE,
    )


def sign_put(key):
    """Подписанная ссылка на запись по ГОТОВОМУ ключу: имена набора HLS заданы манифестом."""
    storage = file_storage()
    return storage.connection.meta.client.generate_presigned_url(
        "put_object",
        Params={"Bucket": storage.bucket_name, "Key": _prefixed(storage, key)},
        ExpiresIn=UPLOAD_MAX_AGE,
    )


def under(prefix):
    """Ключи под префиксом — одним листингом (проверить, что пекарня залила всё)."""
    storage = file_storage()
    if isinstance(storage, FileSystemStorage):
        from .storage import _under

        return set(_under(storage, prefix.strip("/")))
    return {key for key, _, _ in _listing(storage, prefix)}


def listed(prefix):
    """Как `under`, но {ключ: (когда изменён, байт)} — тем же листингом, для уборки."""
    storage = file_storage()
    if isinstance(storage, FileSystemStorage):
        from .storage import _under

        return {key: (storage.get_modified_time(key), storage.size(key)) for key in _under(storage, prefix.strip("/"))}
    return {key: (modified, size) for key, modified, size in _listing(storage, prefix)}


def _listing(storage, prefix):
    """Постраничный обход бакета под префиксом: (ключ без префикса хранилища, дата, вес)."""
    client = storage.connection.meta.client
    keep = f"{storage.location}/" if getattr(storage, "location", "") else ""
    token = {}
    while True:
        answer = client.list_objects_v2(
            Bucket=storage.bucket_name, Prefix=_prefixed(storage, prefix.strip("/")) + "/", **token,
        )
        for item in answer.get("Contents", []):
            yield item["Key"][len(keep):], item.get("LastModified"), item.get("Size", 0)
        if not answer.get("IsTruncated"):
            return
        token = {"ContinuationToken": answer["NextContinuationToken"]}


def _prefixed(storage, key):
    """Ключ так, как он лежит В БАКЕТЕ: с префиксом хранилища, если тот задан."""
    return f"{storage.location}/{key}" if getattr(storage, "location", "") else key


def adopt_token(key, name):
    """Токен, который форма присылает вместо файла. Один и тот же и после одиночного
    PUT, и после сборки из частей — дальше `_adopt` не должен видеть разницы."""
    return signing.dumps({"key": key, "name": name}, salt=UPLOAD_SALT)


# ── Многочастная загрузка ─────────────────────────────────────────────────────
#
# Сырьё лекции — гигабайты: одним PUT его не принять, а за час отдачи связь оборвётся.
# Что уже залито, спрашиваем У ХРАНИЛИЩА, а не у браузера.


def part_size(size):
    """Размер части. 16 МБ, а на гигантском файле — столько, чтобы влезть в 10 000 частей."""
    need = -(-size // MAX_PARTS)  # округление вверх
    return max(PART_SIZE, -(-need // MB) * MB)


def begin_multipart(name, size):
    """Начать многочастную загрузку. Возвращает токен с ключом, номером загрузки и
    объявленным размером: по нему ограничивается число частей и сверяется собранное."""
    key = new_key(name)
    storage = file_storage()
    # Тип у собранного объекта — тот, с которым загрузку начали; части его не несут.
    started = storage.connection.meta.client.create_multipart_upload(
        Bucket=storage.bucket_name, Key=_prefixed(storage, key), ContentType=content_type(key),
    )
    payload = {"key": key, "name": name, "id": started["UploadId"], "size": size}
    return signing.dumps(payload, salt=MULTIPART_SALT)


def multipart(token):
    """Содержимое токена многочастной загрузки или None. Ключ и номер приходят от браузера:
    без подписи он дописался бы в чужую загрузку."""
    try:
        return signing.loads(token, salt=MULTIPART_SALT, max_age=MULTIPART_MAX_AGE)
    except signing.BadSignature:
        return None


def _client(payload):
    storage = file_storage()
    return storage.connection.meta.client, storage.bucket_name, _prefixed(storage, payload["key"])


def part_urls(payload, numbers):
    """Подписанные ссылки на конкретные части — порциями."""
    client, bucket, key = _client(payload)
    return {
        number: client.generate_presigned_url(
            "upload_part",
            Params={"Bucket": bucket, "Key": key, "UploadId": payload["id"], "PartNumber": number},
            ExpiresIn=UPLOAD_MAX_AGE,
        )
        for number in numbers
    }


def uploaded_parts(payload):
    """Части в хранилище: {номер: этаг}, или None — такой загрузки нет (отменили, унесла
    уборка). Отличать от «начата, но пуста» обязательно: мёртвый номер браузер заметил бы
    гигабайтом позже."""
    client, bucket, key = _client(payload)
    done, marker = {}, 0
    while True:
        try:
            answer = client.list_parts(
                Bucket=bucket, Key=key, UploadId=payload["id"], PartNumberMarker=marker,
            )
        except ClientError:
            return None
        for part in answer.get("Parts", []):
            done[part["PartNumber"]] = part["ETag"]
        if not answer.get("IsTruncated"):
            return done
        marker = answer["NextPartNumberMarker"]


class Oversized(Exception):
    """Собранный объект тяжелее объявленного или потолка — и уже снят из хранилища."""


def finish_multipart(payload, parts, limit=math.inf):
    """Собрать объект из частей; токен — как у обычной загрузки. Вес виден только теперь:
    больше объявленного или потолка — объект снимаем сразу (Oversized)."""
    client, bucket, key = _client(payload)
    client.complete_multipart_upload(
        Bucket=bucket, Key=key, UploadId=payload["id"],
        MultipartUpload={"Parts": [
            {"PartNumber": number, "ETag": tag} for number, tag in sorted(parts.items())
        ]},
    )
    size = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
    if size > min(limit, payload.get("size") or math.inf):
        client.delete_object(Bucket=bucket, Key=key)
        raise Oversized(size)
    return adopt_token(payload["key"], payload["name"])


def abort_multipart(payload):
    """Бросить начатое и в хранилище: незаконченные части стоят денег."""
    client, bucket, key = _client(payload)
    client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=payload["id"])


def upload_key(token):
    """Ключ уже залитого файла по токену, или None — где файл не становится `File` (сырьё лекции)."""
    payload = _unsign(token)
    return payload["key"] if payload else None


def uploaded_file(token, folder):
    """(ключ, имя файла) по токену прямой загрузки в эту папку, или None."""
    payload = _unsign(token, folder)
    return (payload["key"], payload["name"]) if payload else None


def _unsign(token, folder=PREFIX):
    """Содержимое токена или None, если подпись не наша, протухла или ключ не из этой папки."""
    try:
        payload = signing.loads(token, salt=UPLOAD_SALT, max_age=UPLOAD_MAX_AGE)
    except signing.BadSignature:
        return None
    return payload if str(payload.get("key", "")).startswith(f"{folder}/") else None


def _stored_size(request, key):
    """Размер объекта в хранилище (None — нет). Кешируем на запрос: спрашивают и check_pending,
    и _adopt."""
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
    """Каждый токен — на реально лежащий файл: иначе книга молча сохранилась бы без него."""
    errors = []
    for token in request.POST.getlist("uploaded"):
        payload = _unsign(token)
        if payload is None:
            errors.append("Один из файлов не доехал до хранилища — загрузи его заново.")
            continue
        size = _stored_size(request, payload["key"])
        limit = max_upload_size(request.user)
        if size is None:
            errors.append(f"«{payload['name']}» не доехал до хранилища — загрузи заново.")
        elif size > limit:
            # Вес в подписи не участвует: ничей платный объект снимаем сразу.
            file_storage().delete(payload["key"])
            errors.append(f"«{payload['name']}» больше {human_size(limit)}")
    return errors


def pending_uploads(request):
    """Токены файлов, залитых до ошибки формы, — обратно в разметку."""
    unsigned = ((token, _unsign(token)) for token in request.POST.getlist("uploaded"))
    return [{"token": token, "name": payload["name"]} for token, payload in unsigned if payload]


def _adopt(token, owner, request, order, name=""):
    """Завести запись на файл в хранилище (проверил check_pending). Ключ, на который запись
    уже есть, второй раз не берём: удаление одной из двух записей сняло бы блоб у обеих."""
    payload = _unsign(token)
    if payload is None or File.objects.filter(file=payload["key"]).exists():
        return None
    size = _stored_size(request, payload["key"])
    if size is None:
        return None

    return File.objects.create(
        **{owner._meta.model_name: owner},
        name=(name or payload["name"])[:150], file=payload["key"], size=size,
        uploader=request.user, order=order,
    )


def sync_images(request, owner):
    """Галерея: удаление помеченных, порядок (image-order) и новые (images) обычным multipart."""
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
        # Исходное имя — только подписью в галерее; у анонимного материала его не покажут.
        Image.objects.create(
            **{owner._meta.model_name: owner},
            name=upload.name[:150], image=scrubbed(upload), uploader=request.user, order=order,
        )
        order += 1


def sync_files(request, owner):
    """Переименование, удаление и порядок файлов + приём новых. FK в File называется как
    модель-владелец (book/material)."""
    for file in owner.files.all():
        if request.POST.get(f"delete-{file.pk}"):
            file.delete()  # post_delete снесёт и сам блоб
            continue
        name = request.POST.get(f"name-{file.pk}", "").strip()
        if name and name != file.name:
            file.name = name[:150]
            file.save(update_fields=["name"])

    # Порядок — по скрытым input name="order".
    by_pk = {file.pk: file for file in owner.files.all()}
    for index, pk in enumerate(request.POST.getlist("order")):
        file = by_pk.get(int(pk)) if pk.isdigit() else None
        if file and file.order != index:
            file.order = index
            file.save(update_fields=["order"])

    # Имена новых — отдельным списком, парами со своим файлом.
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
