"""Спека: что считается готовым файлом для каждого вида вещи.

Договор целиком — в `docs/media-pipeline.md`. Коротко: **сайт не конвертирует.**
Файл печётся снаружи (руками или пекарней на машине с видеокартой), а здесь только
проверяется и отвергается с внятной причиной. Поэтому ни ffmpeg, ни пережатия тут нет
и не будет: проверка — это чтение размеров, а не обработка.

Отсюда же требование к тексту отказа: он должен говорить, что именно не так
(«ждали 16:9, приехало 4:3»), иначе печь придётся наугад.

Когда появится лекторий, эта таблица переедет в общее место и станет отдаваться
пекарне ручкой `GET /intake/spec/` — чтобы рецепт существовал в одном экземпляре.
"""

from collections import namedtuple

from django.core.exceptions import ValidationError

from . import mp4
from .models import CosmeticItem

# ratio — ширина / высота; tolerance — насколько можно ошибиться (доля).
# video — можно ли этому виду быть анимированным видео и с каким потолком веса и длины.
Spec = namedtuple("Spec", "ratio min_width max_bytes tolerance video video_bytes seconds")

K = CosmeticItem.Kind
MB = 1024 * 1024

SPECS = {
    # Рамки достались готовыми и разнокалиберными, поэтому только квадрат и потолок веса.
    # Видео им нельзя: нужен прозрачный проём под лицо (см. CosmeticItem.video).
    K.AVATAR_FRAME: Spec(1 / 1, 112, 6 * MB, 0.02, video=False, video_bytes=0, seconds=0),
    # Шапка тянется на всю ширину карточки профиля (768) при высоте 128.
    K.PROFILE_HEADER: Spec(6 / 1, 768, 4 * MB, 0.02, video=True, video_bytes=2 * MB, seconds=8),
    # Фон кроется по всей контентной области, поэтому обычные пропорции экрана.
    K.PROFILE_BACKGROUND: Spec(16 / 9, 1280, 6 * MB, 0.05, video=True, video_bytes=8 * MB, seconds=8),
}


def human_ratio(ratio):
    """«6:1» вместо «6.0» — в отказе должно быть то же, чем меряет человек."""
    for width, height in ((1, 1), (6, 1), (16, 9), (4, 3), (3, 2), (21, 9)):
        if abs(ratio - width / height) < 0.02:
            return f"{width}:{height}"
    return f"{ratio:.2f}:1"


def check(kind, width, height, size):
    """Причина отказа строкой или None. Ничего не открывает и не читает — только цифры."""
    spec = SPECS.get(kind)
    if spec is None:
        return None

    if not height:
        return "не удалось прочитать размеры картинки"
    got = width / height
    if abs(got - spec.ratio) > spec.ratio * spec.tolerance:
        return f"ждали пропорции {human_ratio(spec.ratio)}, приехало {width}×{height} ({human_ratio(got)})"
    if width < spec.min_width:
        return f"слишком мелко: ждали ширину от {spec.min_width}, приехало {width}"
    if size > spec.max_bytes:
        return f"файл тяжелее {spec.max_bytes // 1024 // 1024} МБ ({size // 1024 // 1024} МБ)"
    return None


def validate(kind, upload):
    """То же самое, но исключением — для форм. Ждёт загруженный файл, а не поле модели."""
    width, height = getattr(upload, "image", None).size if getattr(upload, "image", None) else (0, 0)
    if problem := check(kind, width, height, upload.size):
        raise ValidationError(problem)


def check_video(kind, upload):
    """Причина отказа по видеофайлу или None.

    Читаем заголовок mp4 и сверяем со спекой. Сайт файл не трогает и не чинит: если
    он не по спеке, перепечь его должен тот, у кого видеокарта, — а для этого в отказе
    обязано быть написано, ЧТО именно не так.
    """
    spec = SPECS.get(kind)
    if spec is None:
        return None
    if not spec.video:
        return f"{CosmeticItem.Kind(kind).label} видео не бывает — только картинка"
    if not upload.name.lower().endswith(".mp4"):
        return "ждали mp4: он играет везде, включая айфоны"
    if upload.size > spec.video_bytes:
        return f"видео тяжелее {spec.video_bytes // MB} МБ ({upload.size // MB} МБ)"

    try:
        info = mp4.probe(upload.file)
    except mp4.Broken as error:
        return str(error)

    if not info["faststart"]:
        return "нет faststart: браузер не начнёт играть, пока не скачает файл целиком"
    if info["audio"]:
        return "звуковая дорожка лишняя — это фон под текстом, перепеки с -an"
    if problem := check(kind, info["width"], info["height"], 0):
        return problem
    if info["seconds"] > spec.seconds:
        return f"длиннее {spec.seconds} с ({info['seconds']:.1f} с)"
    return None


def validate_video(kind, upload):
    if problem := check_video(kind, upload):
        raise ValidationError(problem)
