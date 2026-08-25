import json
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import ClientError
from django.conf import settings
from django.core import signing
from django.urls import reverse

from django.core.files.storage import FileSystemStorage

from .media import media_url
from .models import File, Image, human_size
from .storage import file_storage

MB = 1024 * 1024
# Через приложение файл держит воркер и место на диске — лимит скромный.
MAX_FILE_SIZE = 200 * 1024 * 1024
# Одним PUT — до этого. Дальше многочастная загрузка (см. ниже).
MAX_DIRECT_SIZE = 4 * 1024 * 1024 * 1024
# Сырьё лекции: два часа записи — это около 16 ГБ (docs/media-pipeline.md), и меньше
# у камеры не выходит. Остальным столько незачем — место в бакете стоит денег,
# а материалов такого размера не бывает; поэтому потолок даётся по праву на лекторий.
MAX_LECTURE_SIZE = 32 * 1024 * 1024 * 1024

# Больше этого льём частями. Не только ради потолка одиночного PUT: на большом файле
# обрыв связи означал бы начать сначала, а частями докладываются только недостающие.
MULTIPART_FROM = 64 * 1024 * 1024
# Часть. У S3 минимум 5 МБ (кроме последней) и не больше 10 000 частей на объект.
PART_SIZE = 16 * 1024 * 1024
MAX_PARTS = 10000
MULTIPART_SALT = "attachments.multipart"
# Столько живёт начатая загрузка: 16 ГБ с домашнего канала — это больше получаса,
# а с паузами и повторами человек может вернуться к ней и назавтра.
MULTIPART_MAX_AGE = 24 * 3600
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


def max_upload_size(user=None):
    """Потолок для этого человека. Право берём строкой, а не импортом модели: приложение
    вложений не должно зависеть от лектория, а строка — это просто настройка."""
    if not direct_upload():
        return MAX_FILE_SIZE
    if user is not None and user.has_perm("lectorium.add_playlist"):
        return MAX_LECTURE_SIZE
    return MAX_DIRECT_SIZE


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


def drop_replaced(form, field="image"):
    """Снять из хранилища картинку, которую форма заменила или убрала.

    Блоб удаляется вместе с записью (post_delete в attachments/storage.py), но запись-то
    жива — прежний файл ей больше не принадлежит, и добраться до него потом будет неоткуда.
    Звать ПОСЛЕ save(): сравниваем то, что было в базе при показе формы, с тем, что стало.
    """
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


def sign_upload(name):
    """Ключ выбирает сервер и подтверждает подписью: иначе к своей книге можно было бы
    прицепить любой чужой объект из бакета, просто прислав его ключ.

    ВАЖНО: по этой ссылке браузер кладёт файл НАПРЯМУЮ в R2, а это другой домен — значит
    в самом бакете должен быть разрешён CORS с домена сайта, иначе браузер не отправит
    даже предзапрос. В коде это не настраивается, только в панели Cloudflare:

        AllowedOrigins  https://knt-mipt.ru  (+ http://127.0.0.1:8000 и localhost для dev)
        AllowedMethods  PUT
        AllowedHeaders  *
        ExposeHeaders   ETag

    Симптом забытого домена — «Не удалось загрузить …» в форме и 403 на OPTIONS: подпись
    и права тут ни при чём, отказывает браузер. На это наступили при переезде с test.

    `ExposeHeaders` нужен многочастной загрузке: ETag части — это расписка, без которой
    объект потом не собрать. Заголовок хранилище присылает всегда, а вот ЧИТАТЬ его
    скрипту браузер даёт только с этого разрешения. Забыть его — значит залить гигабайты
    и споткнуться на последнем шаге, поэтому клиент говорит об этом прямым текстом.
    """
    key = new_key(name)
    storage = file_storage()
    url = storage.connection.meta.client.generate_presigned_url(
        "put_object",
        Params={"Bucket": storage.bucket_name, "Key": _prefixed(storage, key)},
        ExpiresIn=UPLOAD_MAX_AGE,
    )
    return url, adopt_token(key, name)


def new_key(name):
    return f"uploads/{uuid4().hex}/{Path(name).name}"


def sign_download(key):
    """Подписанная ссылка на ЧТЕНИЕ. По ней пекарня забирает сырьё прямо из R2,
    минуя и сайт, и nginx: гигабайты не должны идти через наш процесс."""
    storage = file_storage()
    return storage.connection.meta.client.generate_presigned_url(
        "get_object",
        Params={"Bucket": storage.bucket_name, "Key": _prefixed(storage, key)},
        ExpiresIn=UPLOAD_MAX_AGE,
    )


def sign_put(key):
    """Подписанная ссылка на запись по ГОТОВОМУ ключу.

    В отличие от `sign_upload`, ключ здесь выбирает не она, а тот, кто зовёт: у готового
    набора HLS имена внутри папки заданы манифестом, и придумать их заново нельзя.
    """
    storage = file_storage()
    return storage.connection.meta.client.generate_presigned_url(
        "put_object",
        Params={"Bucket": storage.bucket_name, "Key": _prefixed(storage, key)},
        ExpiresIn=UPLOAD_MAX_AGE,
    )


def under(prefix):
    """Ключи, лежащие под префиксом. Для проверки, что пекарня залила всё обещанное:
    спрашивать про каждый из тысяч кусков отдельно — тысячи запросов."""
    storage = file_storage()
    if isinstance(storage, FileSystemStorage):
        from .storage import _under

        return set(_under(storage, prefix.strip("/")))

    client = storage.connection.meta.client
    keep = f"{storage.location}/" if getattr(storage, "location", "") else ""
    found, token = set(), {}
    while True:
        answer = client.list_objects_v2(
            Bucket=storage.bucket_name, Prefix=_prefixed(storage, prefix.strip("/")) + "/", **token,
        )
        for item in answer.get("Contents", []):
            found.add(item["Key"][len(keep):])
        if not answer.get("IsTruncated"):
            return found
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
# Зачем вообще: сырьё лекции — это гигабайты, одиночный PUT такого не принимает,
# а главное — на сорока минутах отдачи связь оборвётся почти наверняка, и начинать
# сначала человек не станет. Частями докладываются только недостающие.
#
# Что уже залито, спрашиваем У ХРАНИЛИЩА, а не у браузера: только оно знает правду,
# а браузер мог закрыться, потерять localStorage или соврать.


def part_size(size):
    """Размер части. 16 МБ, а на гигантском файле — столько, чтобы влезть в 10 000 частей."""
    need = -(-size // MAX_PARTS)  # округление вверх
    return max(PART_SIZE, -(-need // MB) * MB)


def begin_multipart(name):
    """Начать многочастную загрузку. Возвращает токен, в котором ключ и номер загрузки."""
    key = new_key(name)
    storage = file_storage()
    started = storage.connection.meta.client.create_multipart_upload(
        Bucket=storage.bucket_name, Key=_prefixed(storage, key),
    )
    return signing.dumps({"key": key, "name": name, "id": started["UploadId"]}, salt=MULTIPART_SALT)


def multipart(token):
    """Содержимое токена многочастной загрузки или None.

    Ключ и номер загрузки приходят от браузера, и без подписи он мог бы прислать чужие:
    дописать часть в чужую загрузку или собрать объект по чужому ключу.
    """
    try:
        return signing.loads(token, salt=MULTIPART_SALT, max_age=MULTIPART_MAX_AGE)
    except signing.BadSignature:
        return None


def _client(payload):
    storage = file_storage()
    return storage.connection.meta.client, storage.bucket_name, _prefixed(storage, payload["key"])


def part_urls(payload, numbers):
    """Подписанные ссылки на конкретные части. Выдаём порциями по ходу дела: на 16 ГБ
    частей тысяча, и все ссылки разом — это полмегабайта ответа и лишний риск протухания."""
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
    """Какие части уже лежат в хранилище: {номер: этаг}, или None — такой загрузки нет.

    None случается штатно: загрузку могли отменить или её унесла уборка, а браузер
    держит токен и просит продолжить. Отличать это от «начата, но пуста» обязательно —
    иначе мы отдали бы браузеру мёртвый номер, и он выяснил бы это гигабайтом позже.
    """
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


def finish_multipart(payload, parts):
    """Собрать объект из частей. Возвращает токен как у обычной загрузки."""
    client, bucket, key = _client(payload)
    client.complete_multipart_upload(
        Bucket=bucket, Key=key, UploadId=payload["id"],
        MultipartUpload={"Parts": [
            {"PartNumber": number, "ETag": tag} for number, tag in sorted(parts.items())
        ]},
    )
    return adopt_token(payload["key"], payload["name"])


def abort_multipart(payload):
    """Бросить начатое. Незаконченные части занимают место в бакете и стоят денег,
    поэтому отмену доводим до хранилища, а не просто забываем."""
    client, bucket, key = _client(payload)
    client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=payload["id"])


def upload_key(token):
    """Ключ уже залитого файла по токену формы, или None.

    Нужен там, где файл не становится записью `File`, а уезжает в работу как есть —
    сырьё лекции живёт в задании, а не в списке вложений.
    """
    payload = _unsign(token)
    return payload["key"] if payload else None


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
