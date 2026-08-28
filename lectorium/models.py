"""Лекторий: плейлисты и лекции.

Единица здесь — **плейлист**, а не лекция. Предмет, семестры и преподаватели висят
на нём; лекция знает только своё место в курсе. Отсюда и обязательная привязка:
на проверку поступает плейлист с лекциями (как материал с файлами), значит плейлист
и есть проверяемая вещь, а лекция без него оказалась бы вне модерации и без метаданных
вовсе. Одиночная запись оформляется плейлистом из одной лекции.

Байтов видео тут нет и не будет: лекция хранит только ПРЕФИКС папки с готовым набором
HLS, а печёт его пекарня снаружи (`docs/media-pipeline.md`).
"""

from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone

from attachments.media import hls_url, redirect_url
from core.models import Moderated, Subject, Term
from intake.spec import MASTER, POSTER
from teachers.models import Teacher


def current_year():
    return timezone.now().year


class Playlist(Moderated):
    title = models.CharField("название", max_length=150)
    synopsis = models.TextField("описание", blank=True)

    subject = models.ForeignKey(Subject, verbose_name="предмет", on_delete=models.PROTECT, related_name="playlists")
    teachers = models.ManyToManyField(Teacher, verbose_name="преподаватели", related_name="playlists", blank=True)
    terms = models.ManyToManyField(Term, verbose_name="семестры", related_name="playlists", blank=True)

    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="загрузил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="playlists",
    )
    year = models.PositiveSmallIntegerField("год", default=current_year)
    created = models.DateTimeField("дата добавления", default=timezone.now)

    class Meta:
        verbose_name = "плейлист"
        verbose_name_plural = "плейлисты"
        ordering = ["-year", "-created"]

    def __str__(self):
        return f"#{self.pk}: {self.title}"

    def get_absolute_url(self):
        return reverse("playlist_detail", args=[self.pk])

    def cover(self):
        """Обложка курса — первая ГОТОВАЯ запись: у необработанной картинки ещё нет."""
        return next((one for one in self.lectures.all() if one.prefix), None)


class Lecture(models.Model):
    """Одна запись. Хранит не файл, а префикс папки с набором HLS.

    Набор — это мастер-манифест, манифесты дорожек, обложка и тысячи сегментов.
    Полем `FileField` такое не описать, да и незачем: имена внутри папки известны
    заранее, их пишет пекарня (`intake.spec`), и хватает одного префикса.
    """

    playlist = models.ForeignKey(Playlist, verbose_name="плейлист", on_delete=models.CASCADE, related_name="lectures")
    title = models.CharField("название", max_length=150)
    order = models.PositiveIntegerField("порядок", default=0)

    # Пусто, пока пекарня не отчиталась: запись уже заведена, а набора ещё нет.
    prefix = models.CharField("папка набора", max_length=200, blank=True)
    duration = models.PositiveIntegerField("длительность (с)", default=0)
    created = models.DateTimeField("добавлена", default=timezone.now)

    # Оценка у ЗАПИСИ, а не у курса: курс из двадцати лекций читается разного качества,
    # и лайк на него не отвечал бы ни на один вопрос. Проверяется же и награждается
    # по-прежнему курс целиком — он единица работы, а не единица просмотра.
    liked_users = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="liked_lectures", blank=True)
    disliked_users = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="disliked_lectures", blank=True)

    class Meta:
        verbose_name = "лекция"
        verbose_name_plural = "лекции"
        ordering = ["order", "id"]
        constraints = [
            # Две лекции на одной папке — это удаление первой, уносящее файлы второй.
            # Пустых сколько угодно: столько записей и ждёт очереди одновременно.
            models.UniqueConstraint(
                fields=["prefix"], condition=~models.Q(prefix=""), name="one_lecture_per_folder",
            ),
        ]

    def __str__(self):
        return self.title

    @property
    def manifest_key(self):
        return f"{self.prefix.strip('/')}/{MASTER}"

    @property
    def poster_key(self):
        return f"{self.prefix.strip('/')}/{POSTER}"

    def manifest_url(self):
        return hls_url(self.manifest_key)

    def poster_url(self):
        return redirect_url(self.poster_key)

    def stage(self):
        """Что написать вместо кадра, пока набора нет.

        «Обрабатывается» у записи, до которой пекарня ещё не дошла, — неправда: очередь
        стоит, пока машину с видеокартой не включат, и это бывают сутки. Человек всё это
        время думает, что работа идёт именно над его лекцией, и ждёт её с минуты на минуту.

        Задания может не быть вовсе — запись завели руками в админке, чтобы прицепить
        набор, испечённый отдельно; такая честно ждёт, но не очереди.
        """
        from intake.models import MediaJob

        job = getattr(self, "job", None)
        if job is None:
            return "набор не привязан"
        return {
            MediaJob.Status.WAITING: "в очереди",
            MediaJob.Status.BAKING: "обрабатывается",
            MediaJob.Status.FAILED: "не обработалась",
        }.get(job.status, "обрабатывается")

    def human_duration(self):
        """«1:23:45» или «12:30» — часы показываем, только когда они есть."""
        hours, rest = divmod(int(self.duration), 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


@receiver(post_delete, sender=Lecture, dispatch_uid="lectorium.segments")
def _drop_segments(sender, instance, **kwargs):
    """Снять набор вместе с лекцией.

    `post_delete` в attachments снимает только файловые ПОЛЯ, а тут их нет — есть папка
    на тысячи сегментов. Без этого удалённая лекция оставила бы в бакете гигабайты.

    Через очередь: пара тысяч ключей — это несколько запросов в чужую сеть, и держать
    на них запрос человека незачем. И только `on_commit`: удаление файлов необратимо,
    а транзакция ещё может откатиться, и тогда мы снесли бы набор у живой записи.
    """
    from .tasks import drop_lecture_files

    prefix = instance.prefix
    if prefix:
        transaction.on_commit(lambda: drop_lecture_files.delay(prefix))
