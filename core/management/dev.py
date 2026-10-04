from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class DevCommand(BaseCommand):
    """Команда, которой место только в разработке.

    Сиды заводят выдуманные книги и материалы, а `seed_chats` — переписку между
    настоящими людьми. Пометка «только для разработки» в описании от запуска
    на боевой базе не защищала ничем.
    """

    def execute(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Эта команда только для разработки — на боевой базе её не запускают.")
        return super().execute(*args, **options)
