from datetime import date

from .nav import section as current_section


def section(request):
    """Раздел, в котором человек сейчас, — для подсветки пункта меню (`core/nav.py`)."""
    return {"section": current_section(request)}


def site_theme(request):
    today = date.today()
    theme = "default"

    # Событийные скины (по дате). Включаем по мере готовности:
    # if (today.month == 12 and today.day >= 20) or (today.month == 1 and today.day <= 10):
    #     theme = "newyear"
    # elif today.month == 10 and today.day >= 25:
    #     theme = "halloween"
    # elif today.month == 5 and today.day == 1:
    #     theme = "birthday"

    return {"site_theme": theme}
