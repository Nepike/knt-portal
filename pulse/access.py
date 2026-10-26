"""Кто видит Пульс и как его действия попадают в историю админки.

Пульс — окно, а не новые полномочия: смотреть его даёт PERM, а каждое действие требует
того же права, что и на сайте или в админке. История одна на двоих — LogEntry: по ней
видно, кто что сделал, руками в админке или кнопкой здесь.
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
