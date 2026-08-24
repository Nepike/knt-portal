from .models import CosmeticItem
from .services import outfit


def my_frame(request):
    """Своя рамка для аватара в меню аккаунта.

    Только своя и только там: в лентах комментариев и чатов рамок по-прежнему нет,
    иначе страница пестрит два десятка раз. Запрос один и не на htmx-фрагментах —
    меню они не рисуют.
    """
    if not request.user.is_authenticated or request.headers.get("HX-Request"):
        return {}
    return {"my_frame": outfit(request.user).get(CosmeticItem.Kind.AVATAR_FRAME)}
