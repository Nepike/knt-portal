from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import Moderated, Subject, Term


class Book(Moderated):
    title = models.CharField("заголовок", max_length=100)
    authors = models.CharField("авторы", max_length=150, blank=True)
    year = models.PositiveSmallIntegerField("год издания", null=True, blank=True)

    subjects = models.ManyToManyField(Subject, verbose_name="предметы", related_name="books", blank=True)
    terms = models.ManyToManyField(Term, verbose_name="семестры", related_name="books", blank=True)

    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="загрузил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="books",
    )
    hide_uploader = models.BooleanField("анонимно", default=False)
    created = models.DateTimeField("дата добавления", default=timezone.now)

    # Файлы книги (PDF/сканы) — в приложении attachments (File с FK сюда), доступ: book.files

    class Meta:
        verbose_name = "книга"
        verbose_name_plural = "книги"
        ordering = ["-created"]

    def __str__(self):
        return f"#{self.pk}: {self.title}"

    def get_absolute_url(self):
        return reverse("book_detail", args=[self.pk])
