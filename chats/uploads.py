"""Вложения сообщения: приём, проверка, привязка.

Файлы чата идут через приложение обычным multipart (до 50 МБ на документ, 10 на фото):
запись заводится в том же запросе, сирот не остаётся. Фото — парой «оригинал + миниатюра»,
обе печёт браузер (components.js), воркер не тратится на Pillow.
"""

from attachments.models import File, Image, human_size
from attachments.uploads import MAX_PICTURE_PIXELS, check_name, inspect_picture, too_many_pixels

MAX_FILES = 10  # вложений на одно сообщение
MAX_PHOTO = 10 * 1024 * 1024
MAX_DOC = 50 * 1024 * 1024


def limits():
    """Пределы для компонента чата: правило одно и живёт здесь."""
    return {"files": MAX_FILES, "photo": MAX_PHOTO, "doc": MAX_DOC}


def _picture_problem(upload):
    """Почему файл не годится в фото, или None. Модельный ImageField содержимое не проверяет,
    а файлы приходят из request.FILES напрямую. Точки — как в галерее (MAX_PICTURE_PIXELS)."""
    kind, pixels = inspect_picture(upload)
    if pixels > MAX_PICTURE_PIXELS:
        return too_many_pixels(upload.name)
    if kind is None:
        return f"«{upload.name}» — это не картинка"
    return None


def problems(photos, previews, docs):
    """Причины, по которым такое сообщение принимать нельзя, списком."""
    found = []
    if len(photos) + len(docs) > MAX_FILES:
        found.append(f"За раз можно отправить не больше {MAX_FILES} вложений")
    # Миниатюры — парами с фото; лишние значат, что запрос собран не нашим полем ввода.
    if len(previews) > len(photos):
        found.append("Миниатюр больше, чем фотографий")
    # Миниатюры проверяем наравне с фото: хранилище общее.
    for picture in [*photos, *previews]:
        if picture.size > MAX_PHOTO:
            found.append(f"«{picture.name}» больше {human_size(MAX_PHOTO)}")
        elif problem := _picture_problem(picture):
            found.append(problem)
    for doc in docs:
        if doc.size > MAX_DOC:
            found.append(f"«{doc.name}» больше {human_size(MAX_DOC)}")
        elif reason := check_name(doc.name):
            found.append(reason)
    return found


def attach(message, photos, previews, docs, uploader):
    """Завести записи вложений сохранённого сообщения. `previews` — парами с `photos`;
    без миниатюры в ленте покажется оригинал."""
    for order, photo in enumerate(photos):
        Image.objects.create(
            message=message, name=photo.name[:150], image=photo,
            preview=previews[order] if order < len(previews) else None,
            uploader=uploader, order=order,
        )

    for order, doc in enumerate(docs):
        File.objects.create(
            message=message, name=doc.name[:150], file=doc, uploader=uploader, order=order,
        )
