from datetime import date

from django.conf import settings


def beta(request):
    """Плашка «бета» в шапке и ссылки на то, что ещё не готово.

    `beta_locked` — «этому человеку закрытое не показываем». Считаем его не для текущего
    адреса (он-то открыт, раз страница рисуется), а для того, что на ней нарисовано:
    ссылку на профиль в меню аккаунта вести некуда, хотя сама страница с меню открыта.
    Список закрытого — в core/beta.py.
    """
    user = getattr(request, "user", None)
    staff = bool(user and user.is_authenticated and user.is_staff)
    return {"beta": settings.BETA, "beta_locked": settings.BETA and not staff}


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
