from django import template

from ..queue import allowed, pending_count

register = template.Library()


@register.simple_tag(takes_context=True)
def review_pending(context):
    """Сколько ждёт проверки; None — человек не модератор, ссылки быть не должно.

    Тегом, а не контекст-процессором: тот считался бы при КАЖДОМ рендере, включая
    htmx-куски вроде ленты чата, где никакого сайдбара нет. Проверка прав сама по
    себе стоит запроса — платить за неё на опросах ленты незачем.
    """
    user = context["request"].user
    if not user.is_authenticated or not allowed(user):
        return None
    return pending_count(user)
