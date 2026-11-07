from django.apps import AppConfig


class AttachmentsConfig(AppConfig):
    name = "attachments"
    verbose_name = "Вложения"

    def ready(self):
        from PIL import Image

        from .storage import connect_blob_cleanup
        from .uploads import MAX_PICTURE_PIXELS

        connect_blob_cleanup()
        # Страховка для любого кода, что откроет картинку: свыше вдвое большего Pillow
        # откажет сам (по умолчанию — от 179 Мп, а это гигабайт памяти на разбор).
        Image.MAX_IMAGE_PIXELS = MAX_PICTURE_PIXELS
