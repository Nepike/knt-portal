"""Разовая чистка после переноса старого сайта.

Независимые проходы, каждый со своим правилом:

* материалы, от которых не осталось ничего — ни файла, ни картинки, ни текста;
* материалы с файлами, которые давно лежат и которые ни разу не скачали;
* материалы старых лет, которыми почти не пользуются;
* предметы, по которым почти ничего нет, — вместе с их материалами;
* люди, которые давно не заходили и ничего после себя не оставили.

Общая грабля всех правил, из-за которой они выглядят сложнее ожидаемого: **счётчик
скачиваний живёт на файлах**. У материала-галереи и у материала-текста он равен нулю
всегда, и правило «ноль скачиваний» без оговорок вынесло бы конспекты и сканы билетов.

Что удаление тянет за собой:
  материал  → его файлы и картинки, вместе с блобами (сигнал в attachments);
  предмет   → сначала его материалы (Material.subject стоит на PROTECT);
  человек   → его ОТЗЫВЫ (Review.author стоит на CASCADE);
              материалы и книги остаются, у них uploader просто станет пустым.

    manage.py cleanup_legacy
    manage.py cleanup_legacy --apply
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from chats.models import Chat, Message
from core.models import Subject, Team
from materials.models import Material
from users.models import User


class Command(BaseCommand):
    help = "Убирает невостребованные материалы и осевших пользователей. Без --apply только показывает."

    def add_arguments(self, parser):
        parser.add_argument("--inactive-days", type=int, default=365, help="сколько не заходить, чтобы попасть в кандидаты")
        parser.add_argument("--materials-before", type=int, default=2021, help="удалять невостребованные материалы старше этого года")
        parser.add_argument("--uploads-since", type=int, default=2021, help="материал этого года и новее защищает своего автора")
        parser.add_argument("--legacy-year", type=int, default=2015, help="материалы этого года создания и старше — кандидаты")
        parser.add_argument("--legacy-downloads", type=int, default=100, help="скачиваний меньше — кандидат")
        parser.add_argument("--legacy-gallery", type=int, default=5, help="картинок меньше — кандидат")
        parser.add_argument("--subject-materials", type=int, default=4, help="предмет с меньшим числом материалов — кандидат")
        parser.add_argument("--chat-members", type=int, default=5, help="курсовой чат с меньшим числом участников — кандидат")
        parser.add_argument("--apply", action="store_true", help="без него только показывает")

    def handle(self, *args, **options):
        self.apply = options["apply"]
        self.empty_materials()
        self.materials(options["materials_before"])
        self.stale_materials(
            options["legacy_year"], options["legacy_downloads"], options["legacy_gallery"],
        )
        self.subjects(options["subject_materials"])
        self.users(options["inactive_days"], options["uploads_since"])
        # Порядок важен: группы обнуляют людям team, люди выходят из курсовых чатов,
        # и только после этого видно, в каком чате правда никого не осталось.
        self.teams()
        self.course_chats(options["chat_members"])
        self.stdout.write(
            self.style.SUCCESS("удалено") if self.apply
            else self.style.WARNING("ничего не удалено — запусти с --apply")
        )

    def materials(self, before):
        """Невостребованный — это материал С ФАЙЛАМИ, которые ни разу не скачали.

        Одного «ноль скачиваний» мало: счётчик живёт на файлах, а материал бывает
        галереей сканов или просто текстом — у таких ноль стоит всегда, и по нему
        под нож пошли бы конспекты и билеты. Отдельно забираем пустышки: ни файлов,
        ни картинок, ни текста — от них не осталось ничего.
        """
        doomed = (
            Material.objects.annotate(
                downloads=Coalesce(Sum("files__downloads"), 0),
                n_files=Count("files", distinct=True),
                n_images=Count("images", distinct=True),
            )
            .filter(created__year__lt=before, n_images=0)  # галерею счётчик не видит, судить по нему нельзя
            .filter(Q(n_files__gt=0, downloads=0) | Q(n_files=0, text=""))
        )
        pks = list(doomed.values_list("pk", flat=True))
        freed = Material.objects.filter(pk__in=pks).aggregate(
            files=Count("files", distinct=True), images=Count("images", distinct=True),
        )
        self.stdout.write(f"материалы старше {before} года без единого скачивания и пустышки: {len(pks)}")
        self.stdout.write(f"  с ними уйдут файлы: {freed['files']}, картинки: {freed['images']}")
        for material in Material.objects.filter(pk__in=pks).order_by("created")[:5]:
            self.stdout.write(f"    #{material.pk} {material.created.year} «{material.title[:50]}»")
        if self.apply:
            # По одному: bulk-delete не шлёт post_delete, и блобы остались бы в хранилище.
            for material in Material.objects.filter(pk__in=pks).iterator():
                material.delete()

    def empty_materials(self):
        """Ни файла, ни картинки, ни строчки текста — от материала осталось одно название.
        Без оглядки на год: такому нечего показывать ни сейчас, ни в 2017-м."""
        doomed = Material.objects.annotate(
            n_files=Count("files", distinct=True), n_images=Count("images", distinct=True),
        ).filter(n_files=0, n_images=0, text="")
        pks = list(doomed.values_list("pk", flat=True))
        self.stdout.write(f"материалы без всякого содержимого: {len(pks)}")
        if self.apply:
            Material.objects.filter(pk__in=pks).delete()

    def subjects(self, minimum):
        """Предмет, по которому почти ничего нет, — вместе с его материалами.

        Книги не отдаём: у предмета может не быть ни одного материала, но висеть два
        десятка книг («Общая физика» — 23 штуки), и удалив его, мы лишили бы библиотеку
        целой рубрики, а книги — единственной метки. Связи с преподавателями теряются,
        но это лишь подпись в профиле, сам преподаватель остаётся.
        """
        doomed = Subject.objects.annotate(
            n_materials=Count("materials", distinct=True), n_books=Count("books", distinct=True),
        ).filter(n_materials__lt=minimum, n_books=0)

        pks = list(doomed.values_list("pk", flat=True))
        materials = list(Material.objects.filter(subject_id__in=pks).values_list("pk", flat=True))
        keeps = Subject.objects.annotate(
            n_materials=Count("materials", distinct=True), n_books=Count("books", distinct=True),
        ).filter(n_materials__lt=minimum, n_books__gt=0)

        self.stdout.write(f"предметы, где материалов меньше {minimum} и нет книг: {len(pks)}")
        self.stdout.write(f"  с ними уйдут материалы: {len(materials)}")
        self.stdout.write(f"  оставлены из-за книг: {keeps.count()} (в них книг: {sum(s.n_books for s in keeps)})")
        if self.apply:
            # Материалы раньше предмета: Material.subject стоит на PROTECT, и по одному —
            # ради post_delete, который снимает блобы файлов и картинок.
            for material in Material.objects.filter(pk__in=materials).iterator():
                material.delete()
            Subject.objects.filter(pk__in=pks).delete()

    def stale_materials(self, year, downloads, gallery):
        """Материалы старых лет, которыми почти не пользуются.

        Год берём из поля year — это год самого материала (лекции такого-то курса),
        а не дата появления на сайте: сайт наполняли с июля 2017-го, и по дате
        добавления раньше 2015 нет вообще ничего.

        Порог здесь режет и то, что ещё берут (у части кандидатов под сотню скачиваний),
        — так решено осознанно ради 15 ГБ, которые иначе пришлось бы тянуть с упавшего
        сервера и хранить.
        """
        doomed = Material.objects.annotate(
            downloads=Coalesce(Sum("files__downloads"), 0),
            n_images=Count("images", distinct=True),
        ).filter(year__lt=year, downloads__lt=downloads, n_images__lt=gallery)

        pks = list(doomed.values_list("pk", flat=True))
        # Имена алиасов не должны повторять названия связей: alias files затенил бы
        # само отношение, и files__downloads стал бы искать поле внутри числа.
        stats = Material.objects.filter(pk__in=pks).aggregate(
            n_files=Count("files", distinct=True), n_images=Count("images", distinct=True),
            total=Coalesce(Sum("files__downloads"), 0),
        )
        self.stdout.write(
            f"материалы старше {year} года, скачиваний < {downloads}, картинок < {gallery}: {len(pks)}"
        )
        self.stdout.write(
            f"  с ними уйдут файлы: {stats['n_files']}, картинки: {stats['n_images']}, "
            f"скачиваний в истории: {stats['total']}"
        )
        if self.apply:
            for material in Material.objects.filter(pk__in=pks).iterator():
                material.delete()

    def teams(self):
        """Учебные группы, которые уже выпустились.

        Судим по выпуску, а не по числу людей: мелкие группы бывают и живыми — Б07-302
        из семи человек выпускается в 2029-м, и столько же в ней было до всякой чистки.
        Отняв у её студентов группу, мы вынули бы их и из чата курса.

        Год выпуска берём у самой модели (graduation_year), чтобы правило «бакалавр
        шесть лет, магистр два» жило в одном месте. Шестьдесят с небольшим строк —
        цикл в питоне тут дешевле, чем выражение в SQL.

        У User.team стоит PROTECT, поэтому сперва снимаем группу с людей, и снимаем
        через save(), а не update(): иначе сигнал не отработает и человек останется
        висеть в чате выпустившегося курса.
        """
        this_year = date.today().year
        doomed = [
            t for t in Team.objects.all()
            # Служебную группу выпускников не трогаем: по обычному расчёту её «выпуск»
            # приходится на шестой год нашей эры, и она вылетела бы первой же.
            if t.year_of_admission != Team.ALUMNI_YEAR and t.graduation_year() < this_year
        ]
        pks = [t.pk for t in doomed]
        people = User.objects.filter(team_id__in=pks)

        self.stdout.write(f"учебные группы, которые выпустились до {this_year}: {len(pks)}")
        self.stdout.write(f"  людей останется без группы: {people.count()}")
        self.stdout.write(f"  групп остаётся: {Team.objects.count() - len(pks)}")
        if self.apply:
            for user in people.iterator():
                user.team = None
                user.save()  # сигнал уберёт членство в чате выпустившегося курса
            Team.objects.filter(pk__in=pks).delete()

    def course_chats(self, minimum):
        """Чат курса, в котором почти никого не осталось. Сообщения уйдут каскадом,
        last_message на Chat стоит SET_NULL и удалению не мешает."""
        doomed = (
            Chat.objects.filter(kind="course")
            .annotate(n_members=Count("memberships", distinct=True))
            .filter(n_members__lt=minimum)
        )
        pks = list(doomed.values_list("pk", flat=True))
        messages = Message.objects.filter(chat_id__in=pks).count()
        self.stdout.write(f"курсовые чаты, где участников меньше {minimum}: {len(pks)}")
        self.stdout.write(f"  с ними уйдут сообщения: {messages}")
        if self.apply:
            Chat.objects.filter(pk__in=pks).delete()

    def users(self, days, uploads_since):
        """Правило: давно не заходил И не оставил после себя ничего живого.

        Живым считаем текстовый отзыв (его читают) и материал не старше uploads_since
        (под ним стоит имя). Оценки без текста автора не спасают — так решено осознанно,
        вместе с ними уходит заметная часть рейтингов.
        """
        cutoff = timezone.now() - timedelta(days=days)
        # Учащихся не трогаем вовсе. Первокурсник, которому завели аккаунт в сентябре и
        # который ещё ни разу не зашёл, формально «неактивен год», но он не выпускник:
        # удалив его, мы заставим старосту заводить человека заново.
        studying = Q(team__year_of_admission__gte=timezone.now().year - 6) & Q(team__stage="bachelor")
        studying |= Q(team__year_of_admission__gte=timezone.now().year - 2) & Q(team__stage="master")
        doomed = (
            User.objects.filter(Q(last_login__lt=cutoff) | Q(last_login__isnull=True))
            .exclude(studying)
            .exclude(is_staff=True).exclude(is_superuser=True)
            .annotate(
                texts=Count("teacher_reviews", filter=~Q(teacher_reviews__text=""), distinct=True),
                fresh=Count("materials", filter=Q(materials__created__year__gte=uploads_since), distinct=True),
            )
            .filter(texts=0, fresh=0)
        )
        pks = list(doomed.values_list("pk", flat=True))
        losing = User.objects.filter(pk__in=pks).aggregate(
            reviews=Count("teacher_reviews", distinct=True),
            materials=Count("materials", distinct=True),
        )
        self.stdout.write(f"не заходили больше {days} дней и ничего не оставили: {len(pks)}")
        self.stdout.write(f"  с ними удалятся отзывы (в т.ч. с оценками): {losing['reviews']}")
        self.stdout.write(f"  материалов осиротеет (останутся, автор станет пустым): {losing['materials']}")

        by_year = (
            User.objects.filter(pk__in=pks).values("team__year_of_admission")
            .annotate(n=Count("id")).order_by("team__year_of_admission")
        )
        self.stdout.write("  по годам поступления: " + ", ".join(
            f"{row['team__year_of_admission']}: {row['n']}" for row in by_year
        ))
        if self.apply:
            User.objects.filter(pk__in=pks).delete()
