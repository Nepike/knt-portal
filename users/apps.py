from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = 'users'
    verbose_name = "Пользователи"

    def ready(self):
        from . import sessions  # noqa: F401 — подписка на вход живёт там
