"""Уведомления: колокольчик в шапке.

Повод — вопросы под материалами, которые никто не видел: на 9 ноя 2026 под чужими
материалами было пять комментариев и ни одного ответа автора. Автору незачем заходить
на свой материал, и узнать о вопросе ему было неоткуда.

Про что уведомление — ключ на саму вещь, отдельный на каждый вид, как у закладок и
комментариев: удалили материал или лекцию — их уведомления уходят вместе с ними.
"""

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

# Виды вещей. Имя вида — имя модели и оно же имя поля (как в `bookmarks`).
THINGS = ("material", "book", "playlist", "lecture")


class Announcement(models.Model):
    """Объявление из Пульса: всем, курсам или группам.

    Каждому получателю — обычная строка `Notification` со ссылкой сюда: колокольчик,
    «прочитано» и уборка работают как у остальных уведомлений, а удалённое объявление
    уносит свои строки у всех разом. Триста строк на объявление базе не в тягость, и
    живут они те же 90 дней.
    """

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
        if self.lecture_id:
            url = reverse("playlist_detail", args=[self.lecture.playlist_id]) + f"?lecture={self.lecture_id}"
        elif self.thing is not None:
            url = self.thing.get_absolute_url()
        else:
            return reverse("notification_list")
        if self.kind in self.GROUPED:
            url += f"#comment-{self.comment_id}" if self.comment_id else "#comments"
        return url
