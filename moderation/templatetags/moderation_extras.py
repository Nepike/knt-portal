from django import template

from ..queue import allowed, pending_count

register = template.Library()


@register.simple_tag(takes_context=True)
def review_pending(context):
    """Сколько ждёт проверки; None — не модератор. Тегом, а не контекст-процессором: тот
    считался бы и на каждом htmx-куске."""
    user = context["request"].user
    if not user.is_authenticated or not allowed(user):
        return None
    return pending_count(user)
