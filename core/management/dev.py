from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class DevCommand(BaseCommand):
    """Команда только для разработки: вне DEBUG отказывается запускаться."""

    def execute(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Эта команда только для разработки — на боевой базе её не запускают.")
        return super().execute(*args, **options)
