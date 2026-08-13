"""Служебная группа выпускников — куда девать людей без потока.

После чистки старого сайта осталось несколько десятков человек, чьи учебные группы
уже выпустились: их сохранили ради текстовых отзывов и материалов. Без группы человек
не попадает ни в один чат, а под его отзывом не рисуется подпись.

Год поступления у группы нулевой (Team.ALUMNI_YEAR) — это метка «потока нет»: по ней
и подпись становится просто «Выпускник», и чат называется «Выпускники», и чистка
учебных групп такую группу обходит.

    manage.py alumni_team
    manage.py alumni_team --apply
"""

from django.core.management.base import BaseCommand

from core.models import Team
from users.models import User

NUMBER = "000000"


class Command(BaseCommand):
    help = "Заводит группу выпускников и переводит в неё всех, кто остался без группы."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="без него только показывает")

    def handle(self, *args, **options):
        apply = options["apply"]
        homeless = User.objects.filter(team__isnull=True)

        self.stdout.write(f"группа {NUMBER}: {'уже есть' if Team.objects.filter(number=NUMBER).exists() else 'будет создана'}")
        self.stdout.write(f"людей без группы: {homeless.count()}")
        if not apply:
            self.stdout.write(self.style.WARNING("ничего не сделано — запусти с --apply"))
            return

        team, created = Team.objects.get_or_create(
            number=NUMBER,
            defaults={
                "profile": "Выпускники",
                "course_code": NUMBER,
                "stage": "bachelor",
                "year_of_admission": Team.ALUMNI_YEAR,
            },
        )
        moved = 0
        for user in homeless.iterator():
            user.team = team
            user.save()  # именно save(): post_save заводит чат курса и членство в нём
            moved += 1

        self.stdout.write(f"группа {'создана' if created else 'уже была'}, переведено: {moved}")
        self.stdout.write(self.style.SUCCESS("готово"))
