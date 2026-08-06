from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from .storage import media_storage

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
    if instance.material_id:
        return f"materials/{instance.material_id}/files/{filename}"
    if instance.book_id:
        return f"books/{instance.book_id}/files/{filename}"
    return f"files/{filename}"


def image_upload_to(instance, filename):
    if instance.material_id:
        return f"materials/{instance.material_id}/images/{filename}"
    return f"images/{filename}"


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
        if not self.material and not self.book:
            raise ValidationError("Файл должен быть привязан к материалу или книге.")
        if self.material and self.book:
            raise ValidationError("Файл не может быть привязан и к материалу, и к книге одновременно.")

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
    # TODO (shop): product FK на shop.Product (картинка товара) — добавить при создании shop

    name = models.CharField("название", max_length=150, blank=True)
    # TODO (M2): image -> Cloudflare R2 (django-storages), пока локально
    image = models.ImageField("изображение", upload_to=image_upload_to)
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


# Удаляем сам файл из хранилища при удалении записи (иначе остаётся «сирота»).
# Работает и для локального диска, и для R2 — storage.delete() одинаков.
@receiver(post_delete, sender=File)
def delete_file_blob(sender, instance, **kwargs):
    if instance.file:
        instance.file.delete(save=False)


@receiver(post_delete, sender=Image)
def delete_image_blob(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(save=False)
