"""Учёт сессий: кто, откуда и когда заходил. Строку заводит вход, освежает запрос, сносит
каскад от сессии (UserSession).
"""

import time

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.utils import timezone

from core.throttle import client_ip

from .models import UserSession

# Отметка активности в самой сессии: она уже прочитана, лишнего запроса не будет.
SEEN_KEY = "_seen"
# Как часто освежать отметку: запись на каждый запрос — лишняя нагрузка.
REFRESH = 5 * 60


def user_agent(request):
    return request.META.get("HTTP_USER_AGENT", "")[:200]


@receiver(user_logged_in)
def remember(sender, request, user, **kwargs):
    if not request.session.session_key:
        # Вход поверх чужой сессии сбрасывает её, а ключ нужен сейчас — под него заводится запись.
        request.session.save()

    request.session[SEEN_KEY] = time.time()
    UserSession.objects.update_or_create(
        session_id=request.session.session_key,
        defaults={
            "user": user, "ip": client_ip(request) or None, "agent": user_agent(request),
            "created": timezone.now(), "seen": timezone.now(),
        },
    )


class ActivityMiddleware:
    """Освежает отметку активности и адрес не чаще раза в REFRESH. Запись в сессию сдвигает
    и её срок: две недели — от последнего захода."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = request.session
        if request.user.is_authenticated and session.session_key:
            now = time.time()
            if now - session.get(SEEN_KEY, 0) > REFRESH:
                session[SEEN_KEY] = now
                # Адрес — вместе с отметкой: важно, откуда заходят сейчас.
                UserSession.objects.filter(session_id=session.session_key).update(
                    seen=timezone.now(), ip=client_ip(request) or None,
                )
        return self.get_response(request)


def alive(user):
    """Живые сессии человека, свежие сверху."""
    return (
        UserSession.objects
        .filter(user=user, session__expire_date__gt=timezone.now())
        .order_by("-seen")
    )
