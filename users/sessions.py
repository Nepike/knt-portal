"""Учёт сессий: кто, откуда и когда заходил в последний раз.

Строку заводит вход, обновляет — обычный запрос, а сносит её каскад от самой сессии
(см. UserSession в models.py). Отдельного сторожа за протухшими записями поэтому нет.
"""

import time

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.utils import timezone

from core.throttle import client_ip

from .models import UserSession

# Отметка активности в самой сессии: она уже прочитана, лишнего запроса не будет.
SEEN_KEY = "_seen"
# Как часто отметку освежать. Каждый запрос — это лишняя запись в базу на каждую
# картинку и каждый опрос чата; раз в пять минут на списке «мои устройства» не видно.
REFRESH = 5 * 60


def _agent(request):
    return request.META.get("HTTP_USER_AGENT", "")[:200]


@receiver(user_logged_in)
def remember(sender, request, user, **kwargs):
    if not request.session.session_key:
        # Вход поверх чужой сессии сбрасывает её, и ключа сейчас нет — он появился бы
        # только в конце запроса. Нам он нужен здесь: под него заводится запись.
        request.session.save()

    request.session[SEEN_KEY] = time.time()
    UserSession.objects.update_or_create(
        session_id=request.session.session_key,
        defaults={
            "user": user, "ip": client_ip(request) or None, "agent": _agent(request),
            "created": timezone.now(), "seen": timezone.now(),
        },
    )


class ActivityMiddleware:
    """Освежает отметку активности и адрес — не чаще, чем раз в REFRESH.

    Побочный, но желанный эффект: запись в сессию сдвигает и её собственный срок,
    так что две недели теперь считаются от последнего захода, а не от входа.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = request.session
        if request.user.is_authenticated and session.session_key:
            now = time.time()
            if now - session.get(SEEN_KEY, 0) > REFRESH:
                session[SEEN_KEY] = now
                # Адрес обновляем вместе с отметкой: человеку важно, откуда заходят
                # СЕЙЧАС, а не откуда вошли неделю назад.
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
