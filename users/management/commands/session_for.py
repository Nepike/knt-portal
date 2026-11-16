"""Сессия для чужого аккаунта — посмотреть сайт его глазами (users/impersonation.py).

    docker compose exec web python manage.py session_for student@phystech.edu

То же умеет карточка в Пульсе; команда — на случай, когда сайт лежит. Ключ — в куку
приватного окна: «стать собой» нельзя, а всё сделанное подписано ЕГО именем. Срок короткий.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from users.impersonation import DEFAULT_MINUTES, drop, mint
from users.models import User


class Command(BaseCommand):
    help = "Выдать ключ сессии для указанного пользователя (посмотреть сайт его глазами)"

    def add_arguments(self, parser):
        parser.add_argument("who", help="почта или id пользователя")
        parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES, help=f"срок жизни, по умолчанию {DEFAULT_MINUTES}")
        parser.add_argument("--end", action="store_true", help="не выдавать новую, а закрыть все сессии этого человека")

    def handle(self, *args, **options):
        user = self.find(options["who"])

        if options["end"]:
            self.stdout.write(f"закрыто сессий: {drop(user)}")
            return

        minutes = max(options["minutes"], 1)
        key = mint(user, minutes)

        self.stdout.write(f"\n{user.full_name} <{user.email}>")
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
