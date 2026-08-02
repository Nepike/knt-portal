from .models import unread_total


def unread_messages(request):
    # Бейдж живёт в base.html, а htmx-фрагменты его не рендерят (свой счётчик им отдаёт
    # unread_badge) — иначе лишний COUNT на каждый опрос ленты, панели и самого бейджа.
    if not request.user.is_authenticated or request.headers.get("HX-Request"):
        return {}
    return {"unread_total": unread_total(request.user)}
