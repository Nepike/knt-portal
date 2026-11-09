from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "notifications"
    verbose_name = "Уведомления"

    def ready(self):
        from . import signals  # noqa: F401 — подключить приёмник удаления комментария
