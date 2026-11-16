"""Кто видит Пульс и как его действия попадают в историю админки. Смотреть даёт PERM, каждое
действие требует того же права, что на сайте или в админке. История одна — LogEntry.
"""

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.decorators import permission_required

PERM = "core.view_pulse"
GROUP = "Пульс"

pulse_only = permission_required(PERM, raise_exception=True)


def logged(request, obj, message, flag=CHANGE):
    """Запись в историю админки. До удаления: у удалённого уже нет номера."""
    LogEntry.objects.log_actions(
        request.user.pk, [obj], flag, change_message=f"Пульс: {message}", single_object=True,
    )
