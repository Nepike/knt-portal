"""Сессия для чужого аккаунта — посмотреть сайт его глазами (команда session_for и Пульс).

Ключ — пароль на предъявителя: его кладут в куку приватного окна, и до конца срока сайт
считает этим человеком любого, у кого он есть. Поэтому срок короткий, а выдача пишется
в лог (и, из Пульса, в историю админки). Сделанное под такой сессией подписано его именем.
"""

import logging
from importlib import import_module

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.models import Session
from django.utils import timezone

from .models import UserSession

logger = logging.getLogger(__name__)

DEFAULT_MINUTES = 30
# Так сессия называется в «моих устройствах» человека. Без имени выдавшего: часть админов
# студентам не известна, и подпись не должна их выдавать. Кто выдал — в истории админки.
AGENT = "синтетическая сессия"


def mint(user, minutes, agent=AGENT):
    """Сессия ровно та же, что после обычного входа: без хеша пароля (HASH_SESSION_KEY)
    AuthenticationMiddleware сочтёт её протухшей и выкинет на первом же запросе."""
    store = import_module(settings.SESSION_ENGINE).SessionStore()
    store[SESSION_KEY] = str(user.pk)
    store[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
    store[HASH_SESSION_KEY] = user.get_session_auth_hash()
    store.set_expiry(minutes * 60)
    store.create()
    # Приписку заводим и здесь, хотя сигнала входа не было: иначе выданная сессия
    # не попадёт в список «мои устройства» и хозяин аккаунта её не увидит.
    UserSession.objects.create(session_id=store.session_key, user=user, agent=agent)
    logger.warning("Выдана сессия для %s (%s), на %s мин: %s", user.email, user.pk, minutes, agent)
    return store.session_key


def drop(user):
    """Сессии не только выданные здесь, а вообще все — чтобы «закрыть доступ» значило
    именно это. Поэтому идём по живым и расшифровываем каждую: обычный вход приписку
    оставляет (users/sessions.py), но полагаться тут на неё нельзя."""
    doomed = [
        session.session_key
        for session in Session.objects.filter(expire_date__gt=timezone.now())
        if session.get_decoded().get(SESSION_KEY) == str(user.pk)
    ]
    return Session.objects.filter(session_key__in=doomed).delete()[0]
