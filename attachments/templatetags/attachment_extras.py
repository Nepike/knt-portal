from django import template

from ..media import file_url, media_url

register = template.Library()


@register.filter
def media(field):
    """{{ image.image|media }} — постоянный адрес картинки, см. attachments/media.py."""
    return media_url(field)


@register.filter
def download_url(file):
    """{{ file|download_url }} — адрес файла со счётчиком, см. attachments/media.py."""
    return file_url(file)
