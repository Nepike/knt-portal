from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = 'users'
    verbose_name = "Пользователи"

    def ready(self):
        from . import journal, sessions  # noqa: F401 — подписки на вход живут там
