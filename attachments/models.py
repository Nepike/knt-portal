from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .storage import media_storage, random_key

# Расширение → вид значка в attachments/_file.html.
FILE_KINDS = {
    "pdf": ("pdf", "djvu"),
    "doc": ("doc", "docx", "odt", "rtf", "txt", "md"),
    "sheet": ("xls", "xlsx", "ods", "csv"),
    "slide": ("ppt", "pptx", "odp"),
    "image": ("jpg", "jpeg", "png", "gif", "webp", "svg", "heic"),
    "archive": ("zip", "rar", "7z", "tar", "gz"),
}


def file_upload_to(instance, filename):
    if instance.message_id:
        return random_key("chats", filename)
    return random_key("materials" if instance.material_id else "books", filename)


def image_upload_to(instance, filename):
    return random_key("chats" if instance.message_id else "images", filename)


def preview_upload_to(instance, filename):
    return random_key("previews", filename)


def human_size(num_bytes):
    if num_bytes is None:
        return ""
    size = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ПБ"


class File(models.Model):
    material = models.ForeignKey("materials.Material", verbose_name="материал", on_delete=models.CASCADE, related_name="files", null=True, blank=True)
    book = models.ForeignKey("library.Book", verbose_name="книга", on_delete=models.CASCADE, related_name="files", null=True, blank=True)
    message = models.ForeignKey("chats.Message", verbose_name="сообщение", on_delete=models.CASCADE, related_name="files", null=True, blank=True)

    name = models.CharField("название", max_length=150)
    file = models.FileField("файл", upload_to=file_upload_to, storage=media_storage)
    size = models.PositiveBigIntegerField("размер (байт)", null=True, blank=True)
    downloads = models.PositiveIntegerField("скачиваний", default=0)
    order = models.PositiveIntegerField("порядок", default=0)

    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="загрузил", on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_files")
    created = models.DateTimeField("создан", default=timezone.now)

    class Meta:
        verbose_name = "файл"
        verbose_name_plural = "файлы"
        ordering = ["order", "id"]

    def __str__(self):
        return self.name

    def clean(self):
        owners = [self.material_id, self.book_id, self.message_id]
        if not any(owners):
            raise ValidationError("Файл должен быть привязан к материалу, книге или сообщению.")
        if sum(bool(owner) for owner in owners) > 1:
            raise ValidationError("У файла может быть только один владелец.")

    def save(self, *args, **kwargs):
        if self.file and not self.size:
            self.size = self.file.size
        super().save(*args, **kwargs)

    def human_size(self):
        return human_size(self.size)

    @property
    def extension(self):
        """Из самого файла, а не из названия: название пишет человек и может соврать."""
        return Path(self.file.name or "").suffix.lstrip(".").lower()

    @property
    def kind(self):
        return next((kind for kind, group in FILE_KINDS.items() if self.extension in group), "other")

    @property
    def label(self):
        """Название без расширения — оно уже нарисовано на значке."""
        suffix = f".{self.extension}"
        return self.name[: -len(suffix)] if self.extension and self.name.lower().endswith(suffix) else self.name


class Image(models.Model):
    material = models.ForeignKey("materials.Material", verbose_name="материал", on_delete=models.CASCADE, related_name="images", null=True, blank=True)
    message = models.ForeignKey("chats.Message", verbose_name="сообщение", on_delete=models.CASCADE, related_name="images", null=True, blank=True)
    # TODO (shop): product FK на shop.Product (картинка товара) — добавить при создании shop

    name = models.CharField("название", max_length=150, blank=True)
    image = models.ImageField("изображение", upload_to=image_upload_to, storage=media_storage)
    # Уменьшенная копия для ленты: снимок с телефона даже после сжатия весит сотни
    # килобайт, и десяток таких в переписке — это мегабайты мобильного трафика ради
    # картинок размером с ноготь. Печёт её браузер вместе с оригиналом.
    preview = models.ImageField("миниатюра", upload_to=preview_upload_to, storage=media_storage, null=True, blank=True)
    size = models.PositiveBigIntegerField("размер (байт)", null=True, blank=True)
    order = models.PositiveIntegerField("порядок", default=0)

    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="загрузил", on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_images")
    created = models.DateTimeField("создан", default=timezone.now)

    class Meta:
        verbose_name = "изображение"
        verbose_name_plural = "изображения"
        ordering = ["order", "id"]

    def __str__(self):
        return self.name or f"изображение #{self.pk}"

    def save(self, *args, **kwargs):
        if self.image and not self.size:
            self.size = self.image.size
        super().save(*args, **kwargs)

    def human_size(self):
        return human_size(self.size)
