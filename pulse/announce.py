"""«Объявления» в Пульсе: разослать в колокольчик и снять.

Смотреть вкладку даёт Пульс, а рассылать — своё право (`notifications.add_announcement`):
объявление получают сотни людей разом, и это решение не каждого, кто видит Пульс.
"""

from django.contrib.admin.models import ADDITION, DELETION
from django.db.models import Count, Q

from core.templatetags.text_extras import plural
from notifications import announcements, services
from notifications.models import Announcement

from .access import logged
from .people import Refused

SEND = "notifications.add_announcement"
REMOVE = "notifications.delete_announcement"
NO_RIGHT = "Для этого нужно право, которого у тебя нет."


def history(limit=30):
    """Последние объявления: сколько получили и сколько уже прочитали."""
    return list(
        Announcement.objects.select_related("author").annotate(
            sent=Count("notifications"), read=Count("notifications", filter=Q(notifications__read_at__isnull=False)),
        )[:limit]
    )


def audience(request, data):
    """«Получат 87 человек · 2 курс» — пока объявление пишут."""
    people, label = announcements.recipients(data.get("courses", []), data.get("teams", []), request.user)
    count = people.count()
    return f"Получат {count} {plural(count, 'человек,человека,человек')} · {label}"


def send(request, form):
    if not request.user.has_perm(SEND):
        raise Refused(NO_RIGHT)
    data = form.cleaned_data
    people, _ = announcements.recipients(data["courses"], data["teams"], request.user)
    if not people.exists():
        raise Refused("В этих курсах и группах никого нет — проверь, кому отправляешь.")
    announcement, count = announcements.publish(request.user, data)
    logged(request, announcement, f"объявление разослано, получили {count}", flag=ADDITION)
    return f"Объявление ушло: {count} {plural(count, 'человек,человека,человек')}"


def withdraw(request, announcement):
    if not request.user.has_perm(REMOVE):
        raise Refused(NO_RIGHT)
    logged(request, announcement, "объявление снято у всех", flag=DELETION)  # до удаления: потом не будет номера
    services.withdraw(announcement)
    return "Объявление снято у всех"
