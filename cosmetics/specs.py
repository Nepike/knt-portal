"""Приёмка косметики: годная картинка и годное видео. Числа про видео — в `intake/spec.py`
(их читает и пекарня). Картинки меряются мягко (рамки со старого сайта разнокалиберные),
видео — точно. Отказ говорит, что именно не так. Сайт не конвертирует.
"""

from collections import namedtuple

from django.core.exceptions import ValidationError

from intake import mp4, spec

from .models import CosmeticItem

# ratio — ширина / высота; tolerance — насколько можно ошибиться (доля).
Spec = namedtuple("Spec", "ratio min_width max_bytes tolerance")

K = CosmeticItem.Kind
MB = 1024 * 1024

SPECS = {
    # Рамки достались готовыми и разнокалиберными, поэтому только квадрат и потолок веса.
    K.AVATAR_FRAME: Spec(1 / 1, 112, 6 * MB, 0.02),
    # Шапка тянется на всю ширину карточки профиля (768) при высоте 128.
    K.PROFILE_HEADER: Spec(6 / 1, 768, 4 * MB, 0.02),
    # Фон кроется по всей контентной области, поэтому обычные пропорции экрана.
    K.PROFILE_BACKGROUND: Spec(16 / 9, 1280, 6 * MB, 0.05),
}

# Вид вещи → рецепт в общей спеке. Рамке видео не бывает: нужен прозрачный проём.
VIDEO = {
    K.PROFILE_HEADER: "cosmetic-header",
    K.PROFILE_BACKGROUND: "cosmetic-background",
}


def human_ratio(ratio):
    """«6:1» вместо «6.0» — в отказе должно быть то же, чем меряет человек."""
    for width, height in ((1, 1), (6, 1), (16, 9), (4, 3), (3, 2), (21, 9)):
        if abs(ratio - width / height) < 0.02:
            return f"{width}:{height}"
    return f"{ratio:.2f}:1"


def check(kind, width, height, size):
    """Причина отказа строкой или None. Ничего не открывает и не читает — только цифры."""
    # rule, а не spec: имя spec занято общей спекой из intake.
    rule = SPECS.get(kind)
    if rule is None:
        return None

    if not height:
        return "не удалось прочитать размеры картинки"
    got = width / height
    if abs(got - rule.ratio) > rule.ratio * rule.tolerance:
        return f"ждали пропорции {human_ratio(rule.ratio)}, приехало {width}×{height} ({human_ratio(got)})"
    if width < rule.min_width:
        return f"слишком мелко: ждали ширину от {rule.min_width}, приехало {width}"
    if size > rule.max_bytes:
        return f"файл тяжелее {rule.max_bytes // MB} МБ ({size // MB} МБ)"
    return None


def validate(kind, upload):
    """То же самое, но исключением — для форм. Ждёт загруженный файл, а не поле модели."""
    width, height = getattr(upload, "image", None).size if getattr(upload, "image", None) else (0, 0)
    if problem := check(kind, width, height, upload.size):
        raise ValidationError(problem)


def check_video(kind, upload):
    """Причина отказа по видеофайлу или None. Заголовок читает `intake.mp4`, сверяет общая
    `spec.check`, как у пекарни. Сайт файл не чинит."""
    if kind not in SPECS:
        return None
    recipe = spec.RECIPES.get(VIDEO.get(kind, ""))
    if recipe is None:
        return f"{CosmeticItem.Kind(kind).label} видео не бывает — только картинка"
    if not upload.name.lower().endswith(".mp4"):
        return "ждали mp4: он играет везде, включая айфоны"

    try:
        info = mp4.probe(upload.file)
    except mp4.Broken as error:
        return str(error)
    return spec.check(recipe, info, upload.size)


def validate_video(kind, upload):
    if problem := check_video(kind, upload):
        raise ValidationError(problem)
