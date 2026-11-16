from django.apps import AppConfig


class AttachmentsConfig(AppConfig):
    name = "attachments"
    verbose_name = "Вложения"

    def ready(self):
        from PIL import Image

        from .storage import connect_blob_cleanup
        from .uploads import MAX_PICTURE_PIXELS

        connect_blob_cleanup()
        # Общий потолок Pillow (по умолчанию — 179 Мп, гигабайт памяти на разбор).
        Image.MAX_IMAGE_PIXELS = MAX_PICTURE_PIXELS
