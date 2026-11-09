from . import services


def unread_notifications(request):
    # htmx-кусок колокольчик не рисует — не тратим COUNT, как и чат (chats.context_processors).
    if not request.user.is_authenticated or request.headers.get("HX-Request"):
        return {}
    return {"unread_notifications": services.unread(request.user).count()}
