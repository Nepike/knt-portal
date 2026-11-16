from .models import CosmeticItem
from .services import outfit


def my_frame(request):
    """Своя рамка для аватара в меню аккаунта — только там: в лентах рамок нет. Не на htmx-кусках."""
    if not request.user.is_authenticated or request.headers.get("HX-Request"):
        return {}
    return {"my_frame": outfit(request.user).get(CosmeticItem.Kind.AVATAR_FRAME)}
