from datetime import timedelta

from django import template
from django.utils import timezone
from django.utils.formats import date_format

register = template.Library()


@register.filter
def ago(moment):
    """«только что», «12 мин», «3 ч», «вчера», «5 ноября» — коротко, под колонку справа."""
    now = timezone.localtime()
    moment = timezone.localtime(moment)
    passed = now - moment
    if passed < timedelta(minutes=1):
        return "только что"
    if passed < timedelta(hours=1):
        return f"{passed.seconds // 60} мин"
    if moment.date() == now.date():
        return f"{passed.seconds // 3600} ч"
    if moment.date() == now.date() - timedelta(days=1):
        return "вчера"
    return date_format(moment, "j E" if moment.year == now.year else "j E Y")
