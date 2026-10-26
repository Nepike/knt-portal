from django.contrib.auth.backends import BaseBackend, ModelBackend
from django.core.exceptions import PermissionDenied

from . import journal


class LoginGate(BaseBackend):
    """Шлагбаум перед ModelBackend: исчерпан лимит неудач — пароль даже не проверяем.

    Бэкендом, а не формой входа: authenticate() зовут и вход сайта, и вход админки,
    форма прикрыла бы один. PermissionDenied обрывает обход бэкендов, и Django шлёт
    user_login_failed — его ловит журнал (users/journal.py). Пароль шлагбаум не проверяет
    и никого не впускает: это делает ModelBackend, и сессии записываются на него.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if request is not None and journal.stopped(request, username or kwargs.get("email")):
            raise PermissionDenied
        return None

    def get_user(self, user_id):
        # Сессию на имя первого бэкенда в списке выдают force_login и session_for —
        # человека по ней находит тот же ModelBackend.
        return ModelBackend().get_user(user_id)
