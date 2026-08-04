from .models import unread_total


def unread_messages(request):
    # htmx-фрагменты бейдж не рендерят (его вьюха кладёт счётчик сама) — не тратим COUNT.
    if not request.user.is_authenticated or request.headers.get("HX-Request"):
        return {}
    return {"unread_total": unread_total(request.user)}
