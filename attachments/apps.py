from django.apps import AppConfig


class AttachmentsConfig(AppConfig):
    name = "attachments"
    verbose_name = "Вложения"

    def ready(self):
        from .storage import connect_blob_cleanup

        connect_blob_cleanup()
