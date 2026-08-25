from datetime import date

from django.conf import settings

from .beta import CLOSED


def beta(request):
    """Плашка «бета» в шапке и гашение пунктов, ведущих в закрытое.

    `locked` — словарь по именам урлов (`{% if locked.shop %}`), и составлен он из того же
    списка, что и сам замок: иначе, открыв раздел, легко забыть про пункт меню и оставить
    его серым при работающей странице.
    """
    user = getattr(request, "user", None)
    staff = bool(user and user.is_authenticated and user.is_staff)
    shut = settings.BETA and not staff
    return {
        "beta": settings.BETA,
        "locked": {name: True for name in CLOSED} if shut else {},
    }


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
