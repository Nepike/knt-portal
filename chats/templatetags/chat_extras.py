from django import template

from ..models import REACTIONS

register = template.Library()


@register.filter
def is_own(message, user):
    """{% with own=m|is_own:user %} — в {% with %} выражения запрещены."""
    return message.author_id == user.pk


@register.filter
def reaction_summary(message, user):
    """[(эмодзи, сколько, моя ли)] — группируем prefetched-реакции в Python."""
    grouped = {}
    for r in message.reactions.all():
        entry = grouped.setdefault(r.emoji, [0, False])
        entry[0] += 1
        if r.user_id == user.pk:
            entry[1] = True
    return [(emoji, count, mine) for emoji, (count, mine) in grouped.items()]


@register.simple_tag
def reaction_palette():
    return REACTIONS
