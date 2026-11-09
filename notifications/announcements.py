"""Объявления: кому разослать.

Курсы — тем же разбором, что у фильтра на странице студентов (`users.views._courses`):
курс нигде не хранится, а выводится из года зачисления группы, и второй разбор
разошёлся бы с первым в первый же сентябрь. Курсы и группы складываются; ничего
не выбрано — всем.
"""

from django.db import transaction

from users.models import User
from users.views import _courses as courses

from . import services
from .models import Announcement


def course_choices():
    return [(key, label) for key, (label, _) in courses().items()]


def recipients(chosen, teams, author):
    """(люди, подпись «кому»). Себе объявление не приходит, как и любое уведомление."""
    people = User.objects.filter(is_active=True).exclude(pk=author.pk)
    if not chosen and not teams:
        return people, "всем"
    buckets = courses()
    ids = {team.pk for team in teams} | {pk for key in chosen if key in buckets for pk in buckets[key][1]}
    parts = [buckets[key][0] for key in chosen if key in buckets]
    if teams:
        parts.append(("группы " if len(teams) > 1 else "группа ") + ", ".join(team.number for team in teams))
    return people.filter(team__in=ids), ", ".join(parts)


def publish(author, data):
    """Разослать из провалидированной формы. (объявление, сколько получило)."""
    people, audience = recipients(data["courses"], data["teams"], author)
    with transaction.atomic():
        announcement = Announcement.objects.create(
            text=data["text"], link=data["link"], author=author, audience=audience,
        )
        count = services.announce(announcement, people)
    return announcement, count
