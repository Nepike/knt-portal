from django.conf import settings
from django.utils import timezone

from .nav import section as current_section


def section(request):
    """Раздел, в котором человек сейчас, — для подсветки пункта меню (`core/nav.py`)."""
    return {"section": current_section(request)}


def theme_for(day):
    """Событийный скин на этот день; `default` — обычный вид сайта.

    Оформление скина — блок `.theme-<имя>` в theme/input.css. На очереди, по мере
    готовности: новый год (20 декабря – 10 января) и день рождения (1 мая).
    """
    if day.month == 10:
        return "halloween"
    return "default"


def site_theme(request):
    # День — по TIME_ZONE сайта: скин приходит и уходит в московскую полночь.
    return {"site_theme": settings.SITE_THEME or theme_for(timezone.localdate())}
