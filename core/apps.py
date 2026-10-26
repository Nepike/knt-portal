from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'
    verbose_name = "Справочники"

    def ready(self):
        from . import failures, runs  # noqa: F401 — подписки на задачи очереди: сбои и последний запуск
