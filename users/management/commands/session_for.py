"""Готовая сессия для чужого аккаунта — чтобы посмотреть сайт его глазами.

Кнопки «войти как» на сайте намеренно НЕТ. Веб-эндпоинт пришлось бы охранять от всех,
кто до него дотянется, и ошибка в одной проверке прав означала бы захват любого аккаунта.
Здесь охраны не нужно вовсе: команду может запустить только тот, у кого уже есть доступ
к серверу и базе, а такому человеку эта команда ничего нового не даёт.

    docker compose exec web python manage.py session_for student@phystech.edu

Ключ кладётся в куку браузера — дальше сайт считает вас этим человеком. Отсюда два
следствия, о которых команда и печатает предупреждение:
  * возврата «стать собой обратно» нет: вы меняете свою же куку. Работать надо
    в приватном окне, тогда своя сессия в основном окне остаётся нетронутой;
  * всё сделанное в этот момент принадлежит ЕМУ. Комментарий, отзыв, сообщение в чате
    будут подписаны его именем, и отличить их потом не по чему.

Поэтому срок жизни короткий (по умолчанию полчаса): ключ — это пароль на предъявителя,
и попавший в переписку или на скриншот действует ровно до истечения.
"""

import logging
from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from users.models import User

logger = logging.getLogger(__name__)

DEFAULT_MINUTES = 30


class Command(BaseCommand):
    help = "Выдать ключ сессии для указанного пользователя (посмотреть сайт его глазами)"

    def add_arguments(self, parser):
        parser.add_argument("who", help="почта или id пользователя")
        parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES, help=f"срок жизни, по умолчанию {DEFAULT_MINUTES}")
        parser.add_argument("--end", action="store_true", help="не выдавать новую, а закрыть все сессии этого человека")

    def handle(self, *args, **options):
        user = self.find(options["who"])

        if options["end"]:
            self.stdout.write(f"закрыто сессий: {self.drop(user)}")
            return

        minutes = max(options["minutes"], 1)
        key = self.mint(user, minutes)
        logger.warning("Выдана сессия для %s (%s), на %s мин", user.email, user.pk, minutes)

        self.stdout.write(f"\n{user.name} {user.surname} <{user.email}>")
        self.stdout.write(f"персонал: {'да' if user.is_staff else 'нет'} · до {timezone.localtime(timezone.now() + timedelta(minutes=minutes)):%H:%M}")
        self.stdout.write(f"\n  кука {settings.SESSION_COOKIE_NAME} = {key}")
        self.stdout.write(f"  path /{'  ·  обязательно с флагом Secure' if settings.SESSION_COOKIE_SECURE else ''}\n")

        if user.must_change_password:
            self.stdout.write("\nУ него стоит флаг «сменить пароль» — сайт уведёт на смену пароля, и это не поломка.")
        self.stdout.write(
            "\nОткрывать в ПРИВАТНОМ окне: кука одна на браузер, в обычном вы разлогините сами себя."
            f"\nВсё сделанное под этой сессией будет от его имени. Досрочно закрыть:"
            f"\n  manage.py session_for {user.email} --end\n"
        )

    def find(self, who):
        user = User.objects.filter(pk=who).first() if who.isdigit() else User.objects.filter(email__iexact=who).first()
        if user is None:
            raise CommandError(f"нет такого пользователя: {who}")
        if not user.is_active:
            raise CommandError(f"{user.email} отключён — сессия всё равно не пустит")
        return user

    def mint(self, user, minutes):
        """Сессия ровно та же, что после обычного входа: без хеша пароля (HASH_SESSION_KEY)
        AuthenticationMiddleware сочтёт её протухшей и выкинет на первом же запросе."""
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store[SESSION_KEY] = str(user.pk)
        store[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
        store[HASH_SESSION_KEY] = user.get_session_auth_hash()
        store.set_expiry(minutes * 60)
        store.create()
        return store.session_key

    def drop(self, user):
        """Своих сессий пользователь не перечисляет — id лежит внутри зашифрованных данных,
        поэтому идём по живым и смотрим каждую. Их десятки, а не миллионы."""
        doomed = [
            session.session_key
            for session in Session.objects.filter(expire_date__gt=timezone.now())
            if session.get_decoded().get(SESSION_KEY) == str(user.pk)
        ]
        return Session.objects.filter(session_key__in=doomed).delete()[0]
