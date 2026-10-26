"""Журнал входов (LoginEvent) и ограничитель неудачных попыток.

Считаются только неудачи: студенты из общежития выходят в сеть через один адрес, и
лимит на любые попытки они выбирали бы друг за друга. Счёт двойной — по паре «аккаунт +
адрес» и по одному адресу. Пара без адреса дала бы чужому запереть человека из дома,
просто набрав его почту десять раз.

Останавливает попытку LoginGate (users/backends.py) до проверки пароля, а записывают
всё сигналы входа — отсюда.
"""

from hashlib import sha256

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from core.throttle import client_ip, exhausted, forgive, strike, throttled

from .models import LoginEvent, User
from .sessions import user_agent

FAILS_PER_ACCOUNT = 10
FAILS_PER_IP = 100
WINDOW = 3600
# Флаг на запросе: эту попытку остановил ограничитель, а не неверный пароль.
STOPPED = "_login_stopped"


def _account_key(email, ip):
    # Отпечаток вместо адреса: в ключ Redis уехало бы то, что набрали в поле почты.
    digest = sha256(email.strip().lower().encode()).hexdigest()[:16]
    return f"login:fail:{digest}:{ip}"


def _ip_key(ip):
    return f"login:fail:ip:{ip}"


def stopped(request, email):
    """Лимит неудач выбран — попытку не проверяем, а запрос помечаем для сигнала."""
    ip = client_ip(request)
    if exhausted(_ip_key(ip), FAILS_PER_IP) or exhausted(_account_key(email or "", ip), FAILS_PER_ACCOUNT):
        setattr(request, STOPPED, True)
        return True
    return False


def forget_failures(request, email):
    """Счёт пары обнуляется входом и сменой пароля по ссылке: иначе исправившийся
    человек сидел бы взаперти до конца часа."""
    forgive(_account_key(email, client_ip(request)))


def _write(kind, request, user):
    LoginEvent.objects.create(
        user=user, kind=kind, ip=client_ip(request) or None, agent=user_agent(request),
    )


@receiver(user_logged_in)
def logged_in(sender, request, user, **kwargs):
    forget_failures(request, user.email)
    _write(LoginEvent.Kind.LOGIN, request, user)


@receiver(user_logged_out)
def logged_out(sender, request, user, **kwargs):
    if user is not None:
        _write(LoginEvent.Kind.LOGOUT, request, user)


@receiver(user_login_failed)
def failed(sender, credentials, request=None, **kwargs):
    # Без запроса authenticate зовут из кода (client.login, команды) — там нет ни адреса,
    # ни перебора.
    if request is None:
        return
    email = credentials.get("username") or credentials.get(User.USERNAME_FIELD) or ""
    user = User.objects.filter(email__iexact=email.strip()).first() if email else None
    ip = client_ip(request)
    if getattr(request, STOPPED, False):
        # Остановленный перебор долбит дальше — пишем одну строку на окно, а не на попытку.
        if not throttled(f"login:stopped:{_account_key(email, ip)}", 1, WINDOW):
            _write(LoginEvent.Kind.STOPPED, request, user)
        return
    strike(_ip_key(ip), WINDOW)
    strike(_account_key(email, ip), WINDOW)
    _write(LoginEvent.Kind.FAILED, request, user)
