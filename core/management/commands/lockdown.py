from django.contrib.auth.backends import ModelBackend
from django.core.management.base import BaseCommand

from core.lockdown import PASS
from core.models import Lockdown


class Command(BaseCommand):
    """Запасной выключатель: админка, где он живёт, сама стоит за заслоном."""

    help = "Закрытие сайта: без ключей — состояние и кто проходит, --on закрыть, --off открыть."

    def add_arguments(self, parser):
        switch = parser.add_mutually_exclusive_group()
        switch.add_argument("--on", action="store_true", help="закрыть сайт")
        switch.add_argument("--off", action="store_true", help="открыть сайт")

    def handle(self, *args, on, off, **options):
        lock = Lockdown.load()
        if on or off:
            lock.active = on
            lock.save()
        self.stdout.write(str(lock))
        for user in ModelBackend().with_perm(PASS).order_by("surname", "name"):
            self.stdout.write(f"  проходит: {user.full_name} <{user.email}>")
