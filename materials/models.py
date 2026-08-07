from django.conf import settings
from django.db import models
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from attachments.storage import media_storage
from core.models import Moderated, Subject, Term
from teachers.models import Teacher


def current_year():
    return timezone.now().year


class Material(Moderated):
    title = models.CharField("заголовок", max_length=100)
    synopsis = models.TextField("описание", blank=True)
    # TODO (frontend): редактор форматированного текста (формат хранения уточним позже)
    text = models.TextField("текст", blank=True)

    subject = models.ForeignKey(Subject, verbose_name="предмет", on_delete=models.PROTECT, related_name="materials")
    teachers = models.ManyToManyField(Teacher, verbose_name="преподаватели", related_name="materials", blank=True)
    terms = models.ManyToManyField(Term, verbose_name="семестры", related_name="materials", blank=True)

    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="загрузил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="materials",
    )
    hide_uploader = models.BooleanField("анонимно", default=False)

    year = models.PositiveSmallIntegerField("год", default=current_year)
    created = models.DateTimeField("дата добавления", default=timezone.now)

    # Файлы и изображения материала — в приложении attachments (File/Image с FK сюда),
    # доступ по related_name: material.files, material.images

    class Meta:
        verbose_name = "материал"
        verbose_name_plural = "материалы"
        ordering = ["-created"]

    def __str__(self):
        return f"#{self.pk}: {self.title}"

    def get_absolute_url(self):
        return reverse("material_detail", args=[self.pk])


class CommentQuerySet(models.QuerySet):
    def with_like_counts(self):
        return self.annotate(
            likes_count=Count("liked_users", distinct=True),
            dislikes_count=Count("disliked_users", distinct=True),
        )


class Comment(models.Model):
    material = models.ForeignKey(Material, verbose_name="материал", on_delete=models.CASCADE, related_name="comments")
    parent = models.ForeignKey(
        "self", verbose_name="ответ на", on_delete=models.CASCADE,
        null=True, blank=True, related_name="replies",
    )
    author = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="автор", on_delete=models.CASCADE, related_name="material_comments")
    hide_author = models.BooleanField("анонимно", default=False)

    text = models.TextField("текст", blank=True)
    image = models.ImageField(
        "изображение", upload_to="comments/images", storage=media_storage, null=True, blank=True,
    )

    liked_users = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="liked_comments", blank=True)
    disliked_users = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="disliked_comments", blank=True)

    created = models.DateTimeField("создан", default=timezone.now)

    objects = CommentQuerySet.as_manager()

    class Meta:
        verbose_name = "комментарий"
        verbose_name_plural = "комментарии"
        ordering = ["created"]

    def __str__(self):
        return f"{self.author} → {self.material}"
