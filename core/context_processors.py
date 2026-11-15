from datetime import date, datetime, time

from django.conf import settings
from django.utils import timezone

from .nav import section as current_section

# Глитч-скин «сайт взломан» — разовый, на 2026 год. Отсчёт идёт до полуночи 1 декабря
# по Москве (TIME_ZONE сайта); этот день — «красный», отсчёт стоит на нуле. 2 декабря —
# праздник: сайт «освобождён». Сам отсчёт и квест — приложение quest.
GLITCH_FROM = date(2026, 11, 18)
ZERO_DAY = date(2026, 12, 1)
PARTY_DAY = date(2026, 12, 2)


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
    if GLITCH_FROM <= day <= ZERO_DAY:
        return "glitch"
    if day == PARTY_DAY:
        return "party"
    return "default"


def skin_classes(theme, day):
    """Классы <html> на этот день. Глитч бывает только тёмным: `dark` ставит сервер, а
    переключатель темы на это время заперт. Выбор человека в localStorage не трогаем —
    его светлая тема вернётся сама, когда скин кончится."""
    classes = [f"theme-{theme}"]
    if theme == "glitch":
        classes.append("dark")
        if day == ZERO_DAY:
            classes.append("redday")
    return " ".join(classes)


def today():
    """Скин и классы <html> на сегодня. День — по TIME_ZONE сайта: скин приходит и уходит
    в московскую полночь. `SITE_THEME` в настройках ставит скин принудительно; «redday» —
    глитч в его последний, красный день: так 1 декабря смотрят до срока."""
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
