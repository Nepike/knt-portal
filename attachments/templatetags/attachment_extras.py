from django import template

from ..media import media_url

register = template.Library()


@register.filter
def media(field):
    """{{ image.image|media }} — постоянный адрес картинки, см. attachments/media.py."""
    return media_url(field)
