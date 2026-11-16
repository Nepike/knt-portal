"""Закладки: «вернуться к этому потом». Помечается ВЕЩЬ, а не адрес: удалили — закладка
ушла, переименовали — в списке новое имя. Ключ на каждый вид (как у attachments.File):
GenericForeignKey не держит целостность в базе.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

# Виды владельцев: имя модели — оно же имя поля (см. `views.owners`).
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
            # По одной закладке на вещь; NULL в Postgres друг другу не равны — пустые поля не мешают.
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
