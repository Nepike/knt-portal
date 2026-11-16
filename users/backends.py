from django.contrib.auth.backends import BaseBackend, ModelBackend
from django.core.exceptions import PermissionDenied

from . import journal


class LoginGate(BaseBackend):
    """Шлагбаум перед ModelBackend: лимит неудач выбран — пароль не проверяем. Бэкендом, а не
    формой: authenticate() зовут и сайт, и админка. PermissionDenied обрывает обход бэкендов,
    user_login_failed ловит журнал (users/journal.py). Впускает только ModelBackend."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if request is not None and journal.stopped(request, username or kwargs.get("email")):
            raise PermissionDenied
        return None

    def get_user(self, user_id):
        # Сессию на имя первого бэкенда выдают force_login и session_for — человека находит ModelBackend.
        return ModelBackend().get_user(user_id)
