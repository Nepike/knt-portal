from django.core.management.base import BaseCommand

from printing.cleanup import sweep


class Command(BaseCommand):
    help = "Убирает файлы печати: задания, законченные больше суток назад, и бесхозные папки printing/."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="без него только показывает, что сделал бы")

    def handle(self, *args, **options):
        expired, folders, stray = sweep(apply=options["apply"], say=self.stdout.write)
        verdict = "сделано" if options["apply"] else "нашлось (запусти с --apply)"
        self.stdout.write(self.style.SUCCESS(
            f"{verdict}: истекло ожидание у {expired}, папок заданий {folders}, бесхозных {stray}"
        ))
