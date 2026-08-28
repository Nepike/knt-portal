"""Закладки: «вернуться к этому потом».

Помечается не адрес, а ВЕЩЬ — материал, книга, курс лекций, преподаватель. Строкой
с путём было бы проще, но такая закладка живёт своей жизнью: материал удалили, а строка
осталась и ведёт в 404; переименовали — а в списке старое название. Ключ на настоящую
запись снимается вместе с ней сам и показывает её нынешнее имя.

Владелец — отдельный ключ на каждый вид, как у `attachments.File` и `comments.Comment`.
`GenericForeignKey` тут напрашивается сильнее всего, и он же тут хуже всего: закладка —
это ровно та вещь, которую некому потом убрать за удалённым владельцем, а целостности
на уровне базы у него нет.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

# Виды владельцев. Имя вида — это имя модели (`_meta.model_name`) и оно же имя поля:
# по нему закладка и находится, не зная, с чем имеет дело (см. `views.owners`).
KINDS = ("material", "book", "playlist", "teacher")


class Bookmark(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.CASCADE, related_name="bookmarks",
    )

    material = models.ForeignKey(
        "materials.Material", verbose_name="материал", on_delete=models.CASCADE,
        related_name="bookmarks", null=True, blank=True,
    )
    book = models.ForeignKey(
        "library.Book", verbose_name="книга", on_delete=models.CASCADE,
        related_name="bookmarks", null=True, blank=True,
    )
    playlist = models.ForeignKey(
        "lectorium.Playlist", verbose_name="курс лекций", on_delete=models.CASCADE,
        related_name="bookmarks", null=True, blank=True,
    )
    teacher = models.ForeignKey(
        "teachers.Teacher", verbose_name="преподаватель", on_delete=models.CASCADE,
        related_name="bookmarks", null=True, blank=True,
    )

    created = models.DateTimeField("добавлена", default=timezone.now)

    class Meta:
        verbose_name = "закладка"
        verbose_name_plural = "закладки"
        ordering = ["-created"]
        constraints = [
            # По одной закладке на вещь. Пустых сколько угодно: у закладки на книгу
            # поле material пусто, а NULL в Postgres друг другу не равны — именно на это
            # и рассчитано, иначе второй вид перестал бы помещаться в таблицу.
            models.UniqueConstraint(fields=["user", kind], name=f"one_bookmark_per_{kind}")
            for kind in KINDS
        ]

    def __str__(self):
        return f"{self.user} → {self.owner}"

    @property
    def owner(self):
        """Помеченная вещь, какого бы она ни была вида."""
        return self.material or self.book or self.playlist or self.teacher

    @property
    def kind(self):
        """Вид владельца строкой — им же адресуется ручка (`bookmarks/urls.py`)."""
        return next((kind for kind in KINDS if getattr(self, f"{kind}_id")), "")

    def clean(self):
        owners = [kind for kind in KINDS if getattr(self, f"{kind}_id")]
        if not owners:
            raise ValidationError("Закладка должна указывать на материал, книгу, курс или преподавателя.")
        if len(owners) > 1:
            raise ValidationError("Закладка указывает ровно на одну вещь.")
