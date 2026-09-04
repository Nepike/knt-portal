"""Вложения сообщения: приём, проверка, привязка.

Файлы чата идут ЧЕРЕЗ приложение обычным multipart, а не прямой загрузкой в бакет,
как у материалов. Причина в размерах: там сырьё лекции в гигабайтах, и подписанные
ссылки с докладкой частями окупаются, а тут потолок — полсотни мегабайт на документ
и десяток на фото, причём фото браузер ещё и сжимает перед отправкой. Один путь вместо
двух работает одинаково и на проде с R2, и в разработке на диске, и не оставляет
в хранилище сирот: запись заводится в том же запросе, что и загрузка.

Фото приезжает парой «оригинал + миниатюра»: обе печёт браузер (chatFiles в
components.js). Миниатюру не делаем здесь Pillow'ом намеренно — это лишняя работа
воркера на каждое сообщение, при том что у отправителя картинка уже открыта.
"""

from PIL import Image as PilImage

from attachments.models import File, Image, human_size
from attachments.uploads import check_name

MAX_FILES = 10  # вложений на одно сообщение
MAX_PHOTO = 10 * 1024 * 1024
MAX_DOC = 50 * 1024 * 1024


def limits():
    """Пределы для компонента чата: правило одно и живёт здесь."""
    return {"files": MAX_FILES, "photo": MAX_PHOTO, "doc": MAX_DOC}


def _is_picture(upload):
    """Картинка ли это на самом деле.

    Модельный ImageField содержимое НЕ проверяет — это делает только форма, а сюда
    файлы приходят из request.FILES напрямую. Без проверки «фотографией» стал бы любой
    файл с подходящим расширением, и картинки чата превратились бы в дыру, через
    которую в бакет кладут что угодно.
    """
    try:
        PilImage.open(upload).verify()
    except Exception:  # noqa: BLE001 — Pillow бросает чем придётся, и всё это «не картинка»
        return False
    finally:
        upload.seek(0)  # verify дочитал файл до конца, а его ещё сохранять
    return True


def problems(photos, previews, docs):
    """Причины, по которым такое сообщение принимать нельзя, списком."""
    found = []
    if len(photos) + len(docs) > MAX_FILES:
        found.append(f"За раз можно отправить не больше {MAX_FILES} вложений")
    # Миниатюры идут парами с фото, лишним взяться неоткуда. Пришли — значит запрос
    # собран не нашим полем ввода.
    if len(previews) > len(photos):
        found.append("Миниатюр больше, чем фотографий")
    # Миниатюры проверяем наравне с фото. Поле у них своё, а хранилище общее, и без
    # проверки через него уезжал бы файл любого размера и любого содержимого —
    # «картинкой» он оказывался бы только по названию поля, в которое его положили.
    for picture in [*photos, *previews]:
        if picture.size > MAX_PHOTO:
            found.append(f"«{picture.name}» больше {human_size(MAX_PHOTO)}")
        elif not _is_picture(picture):
            found.append(f"«{picture.name}» — это не картинка")
    for doc in docs:
        if doc.size > MAX_DOC:
            found.append(f"«{doc.name}» больше {human_size(MAX_DOC)}")
        elif reason := check_name(doc.name):
            found.append(reason)
    return found


def attach(message, photos, previews, docs, uploader):
    """Завести записи вложений у сохранённого сообщения.

    `previews` идут строго парами с `photos` — они лежат в одной строке формы и
    отправляются вместе. Не доехавшая миниатюра не повод терять фото: в ленте тогда
    покажется оригинал.
    """
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
