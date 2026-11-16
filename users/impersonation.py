"""Сессия для чужого аккаунта — посмотреть сайт его глазами (session_for и Пульс). Ключ —
пароль на предъявителя: срок короткий, выдача пишется в лог.
"""

import logging
from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.models import Session
from django.utils import timezone

from .models import UserSession

logger = logging.getLogger(__name__)

DEFAULT_MINUTES = 30
# Так сессия называется в «моих устройствах». Без имени выдавшего — кто выдал, видно в истории админки.
AGENT = "синтетическая сессия"


def mint(user, minutes, agent=AGENT):
    """Сессия ровно та же, что после обычного входа: без хеша пароля (HASH_SESSION_KEY)
    AuthenticationMiddleware сочтёт её протухшей и выкинет на первом же запросе."""
    store = import_module(settings.SESSION_ENGINE).SessionStore()
    store[SESSION_KEY] = str(user.pk)
    store[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
    store[HASH_SESSION_KEY] = user.get_session_auth_hash()
    # timedelta, а не секунды: число Django считает сроком бездействия, и активность продлевала бы сессию.
    store.set_expiry(timedelta(minutes=minutes))
    store.create()
    # Приписка — иначе сессии не видно в «моих устройствах».
    UserSession.objects.create(session_id=store.session_key, user=user, agent=agent)
    logger.warning("Выдана сессия для %s (%s), на %s мин: %s", user.email, user.pk, minutes, agent)
    return store.session_key


def drop(user):
    """Закрыть ВСЕ сессии человека, не только выданные: идём по живым и расшифровываем каждую."""
    doomed = [
        session.session_key
        for session in Session.objects.filter(expire_date__gt=timezone.now())
        if session.get_decoded().get(SESSION_KEY) == str(user.pk)
    ]
    return Session.objects.filter(session_key__in=doomed).delete()[0]
