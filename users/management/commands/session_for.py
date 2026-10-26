"""Готовая сессия для чужого аккаунта — чтобы посмотреть сайт его глазами.

    docker compose exec web python manage.py session_for student@phystech.edu

Ту же сессию выдаёт и карточка человека в Пульсе — только суперпользователю, с записью
в историю админки (pulse/people.py). Команда остаётся на случай, когда сайт лежит или
суперпользователя под рукой нет: её может запустить лишь тот, у кого уже есть доступ к
серверу, и такому человеку она ничего нового не даёт. Сама выдача — users/impersonation.py.

Ключ кладётся в куку браузера — дальше сайт считает вас этим человеком. Отсюда два
следствия, о которых команда и печатает предупреждение:
  * возврата «стать собой обратно» нет: вы меняете свою же куку. Работать надо
    в приватном окне, тогда своя сессия в основном окне остаётся нетронутой;
  * всё сделанное в этот момент принадлежит ЕМУ. Комментарий, отзыв, сообщение в чате
    будут подписаны его именем, и отличить их потом не по чему.

Поэтому срок жизни короткий (по умолчанию полчаса): ключ — это пароль на предъявителя,
и попавший в переписку или на скриншот действует ровно до истечения.
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
