from datetime import date, datetime, time

from django.conf import settings
from django.utils import timezone

from .nav import section as current_section

# Глитч-скин «сайт взломан» (2026, приложение quest). Отсчёт — до полуночи 1 декабря
# по Москве; 1 декабря — «красный день», 2-го — праздник.
GLITCH_FROM = date(2026, 11, 18)
ZERO_DAY = date(2026, 12, 1)
PARTY_DAY = date(2026, 12, 2)


def section(request):
    """Раздел, в котором человек сейчас, — для подсветки пункта меню (`core/nav.py`)."""
    return {"section": current_section(request)}


def theme_for(day):
    """Событийный скин на этот день; `default` — обычный вид. Оформление — `.theme-<имя>` в theme/input.css."""
    if day.month == 10:
        return "halloween"
    if GLITCH_FROM <= day <= ZERO_DAY:
        return "glitch"
    if day == PARTY_DAY:
        return "party"
    return "default"


def skin_classes(theme, day):
    """Классы <html> на этот день. Глитч только тёмный: `dark` ставит сервер, переключатель
    темы заперт, выбор человека в localStorage не трогаем."""
    classes = [f"theme-{theme}"]
    if theme == "glitch":
        classes.append("dark")
        if day == ZERO_DAY:
            classes.append("redday")
    return " ".join(classes)


def today():
    """Скин и классы <html> на сегодня (день — по TIME_ZONE). `SITE_THEME` ставит скин
    принудительно; «redday» — последний, красный день глитча."""
    day = timezone.localdate()
    if settings.SITE_THEME == "redday":
        return "glitch", skin_classes("glitch", ZERO_DAY)
    theme = settings.SITE_THEME or theme_for(day)
    return theme, skin_classes(theme, day)


def zero():
    """Отсчёт дошёл до нуля: красный день, передача и ввод ключа (quest)."""
    return "redday" in today()[1].split()


def party():
    """Праздник после взлома: праздничная страница (quest) и кейс в подарок (cosmetics.cases)."""
    return today()[0] == "party"


def midnight(day):
    """Начало дня по времени сайта — в миллисекундах, как их считает браузер."""
    return int(timezone.make_aware(datetime.combine(day, time())).timestamp() * 1000)


def site_theme(request):
    theme, classes = today()
    context = {"site_theme": theme, "skin_classes": classes}
    if theme == "glitch":
        context["skin_deadline"] = midnight(ZERO_DAY)  # счётчик в шапке (quest/_pill.html)
    return context
