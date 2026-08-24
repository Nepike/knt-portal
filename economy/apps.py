from django.apps import AppConfig


class EconomyConfig(AppConfig):
    name = "economy"
    verbose_name = "Экономика"

    def ready(self):
        from . import signals  # noqa: F401 — подписка на вход живёт там
