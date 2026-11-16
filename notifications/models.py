"""Уведомления: колокольчик в шапке. Про что — ключ на саму вещь, отдельный на каждый вид
(как у закладок): удалили вещь — её уведомления ушли.
"""

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

# Виды вещей. Имя вида — имя модели и оно же имя поля (как в `bookmarks`).
THINGS = ("material", "book", "playlist", "lecture")


class Announcement(models.Model):
    """Объявление из Пульса: всем, курсам или группам. Каждому получателю — обычная строка
    `Notification`; удалённое объявление уносит свои строки у всех."""

    text = models.TextField("текст", max_length=2000)
    link = models.CharField("ссылка", max_length=300, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    audience = models.CharField("кому", max_length=500)
    created = models.DateTimeField("когда", default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "объявление"
        verbose_name_plural = "объявления"
        ordering = ["-created"]

    def __str__(self):
        return self.text[:60]

    def get_absolute_url(self):
        return reverse("announcement_detail", args=[self.pk])


class Notification(models.Model):
    class Kind(models.TextChoices):
        COMMENT = "comment", "комментарий под вашим"
        REPLY = "reply", "ответ на комментарий"
        APPROVED = "approved", "опубликовано"
        REJECTED = "rejected", "отклонено"
        BAKED = "baked", "лекция готова"
        BAKE_FAILED = "bake_failed", "лекция не обработалась"
        ANNOUNCEMENT = "announcement", "объявление"
        WALL_BAN = "wall_ban", "Стена закрыта"
        WALL_UNBAN = "wall_unban", "Стена открыта"

    # Пока не прочитано, новое того же вида про ту же вещь не добавляет строку, а
    # копит счётчик: десять комментариев под материалом — одна строка «10 новых».
    GROUPED = (Kind.COMMENT, Kind.REPLY)

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кому", on_delete=models.CASCADE, related_name="notifications",
    )
    kind = models.CharField("что", max_length=12, choices=Kind.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    anonymous = models.BooleanField("скрыть кто", default=False)
    times = models.PositiveIntegerField("сколько раз", default=1)
    note = models.CharField("причина", max_length=300, blank=True)

    material = models.ForeignKey("materials.Material", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    book = models.ForeignKey("library.Book", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    playlist = models.ForeignKey("lectorium.Playlist", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    lecture = models.ForeignKey("lectorium.Lecture", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    # Последний из склеенных. Не каскадом: удалённый комментарий снимает строку, только
    # если он в ней один (notifications/signals.py), — иначе пропали бы и остальные.
    comment = models.ForeignKey("comments.Comment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    announcement = models.ForeignKey(
        Announcement, on_delete=models.CASCADE, null=True, blank=True, related_name="notifications",
    )

    created = models.DateTimeField("когда", default=timezone.now, db_index=True)
    read_at = models.DateTimeField("прочитано", null=True, blank=True)

    class Meta:
        verbose_name = "уведомление"
        verbose_name_plural = "уведомления"
        ordering = ["-created"]
        # Колокольчик считает непрочитанное на каждой странице
        indexes = [models.Index(fields=["recipient", "read_at"])]

    def __str__(self):
        return f"{self.recipient} · {self.get_kind_display()}"

    @property
    def thing(self):
        return self.material or self.book or self.playlist or self.lecture

    @property
    def thing_kind(self):
        return next((kind for kind in THINGS if getattr(self, f"{kind}_id")), "")

    def target_url(self):
        """Куда ведёт: к самому комментарию, если он ещё есть, иначе к вещи."""
        if self.announcement_id:
            return self.announcement.get_absolute_url()
        if self.kind in (self.Kind.WALL_BAN, self.Kind.WALL_UNBAN):
            return reverse("wall")
        if self.lecture_id:
            url = reverse("playlist_detail", args=[self.lecture.playlist_id]) + f"?lecture={self.lecture_id}"
        elif self.thing is not None:
            url = self.thing.get_absolute_url()
        else:
            return reverse("notification_list")
        if self.kind in self.GROUPED:
            url += f"#comment-{self.comment_id}" if self.comment_id else "#comments"
        return url
