from datetime import date

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Avg, Count
from django.urls import reverse
from django.utils import timezone

from attachments.storage import media_storage, random_key
from core.models import Subject


def photo_upload_to(instance, filename):
    return random_key("teachers", filename)


def review_image_to(instance, filename):
    return random_key("reviews", filename)

SCORE_LABELS = {
    "score_knowledge": "Знания",
    "score_skill": "Умение преподавать",
    "score_communication": "В общении",
    "score_freeloading": "Халявность",
}


class TeacherQuerySet(models.QuerySet):
    def with_ratings(self):
        # order_by обязателен: annotate с GROUP BY отбрасывает Meta.ordering
        return self.annotate(
            reviews_count=Count("reviews", distinct=True),
            avg_knowledge=Avg("reviews__score_knowledge"),
            avg_skill=Avg("reviews__score_skill"),
            avg_communication=Avg("reviews__score_communication"),
            avg_freeloading=Avg("reviews__score_freeloading"),
        ).order_by("surname", "name")


class Teacher(models.Model):
    name = models.CharField("имя", max_length=50)
    surname = models.CharField("фамилия", max_length=50)
    patronymic = models.CharField("отчество", max_length=50, blank=True)

    bio = models.TextField("о преподавателе", blank=True)
    photo = models.ImageField("фото", upload_to=photo_upload_to, storage=media_storage, null=True, blank=True)
    birthday = models.DateField("дата рождения", null=True, blank=True)

    phone = models.CharField("телефон", max_length=50, blank=True)
    email = models.CharField("почта", max_length=100, blank=True)
    vk_page = models.CharField("VK (без https://vk.com/)", max_length=50, blank=True)
    tg_page = models.CharField("TG (без https://t.me/)", max_length=50, blank=True)

    subjects = models.ManyToManyField(Subject, verbose_name="предметы", related_name="teachers", blank=True)

    objects = TeacherQuerySet.as_manager()

    class Meta:
        verbose_name = "преподаватель"
        verbose_name_plural = "преподаватели"
        ordering = ["surname", "name"]
        constraints = [
            models.UniqueConstraint(fields=["surname", "name", "patronymic"], name="unique_teacher_fio"),
        ]

    def __str__(self):
        return self.short_name()

    def get_absolute_url(self):
        return reverse("teacher_detail", args=[self.pk])

    def short_name(self):
        initials = f"{self.name[0]}." if self.name else ""
        if self.patronymic:
            initials += f"{self.patronymic[0]}."
        return f"{self.surname} {initials}".strip()

    def overall_rating(self):
        parts = [self.avg_knowledge, self.avg_skill, self.avg_communication, self.avg_freeloading]
        rated = [p for p in parts if p is not None]
        return round(sum(rated) / len(rated), 2) if rated else None

    def age(self):
        if not self.birthday:
            return None
        today = date.today()
        return today.year - self.birthday.year - ((today.month, today.day) < (self.birthday.month, self.birthday.day))


class Review(models.Model):
    SCORE_VALIDATORS = [MinValueValidator(1), MaxValueValidator(5)]

    teacher = models.ForeignKey(Teacher, verbose_name="преподаватель", on_delete=models.CASCADE, related_name="reviews")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="автор", on_delete=models.CASCADE, related_name="teacher_reviews")
    hide_author = models.BooleanField("анонимно", default=False)
    text = models.TextField("текст", blank=True)
    image = models.ImageField(
        "изображение", upload_to=review_image_to, storage=media_storage, null=True, blank=True,
    )

    score_knowledge = models.PositiveSmallIntegerField("знания", null=True, blank=True, validators=SCORE_VALIDATORS)
    score_skill = models.PositiveSmallIntegerField("умение преподавать", null=True, blank=True, validators=SCORE_VALIDATORS)
    score_communication = models.PositiveSmallIntegerField("общение", null=True, blank=True, validators=SCORE_VALIDATORS)
    score_freeloading = models.PositiveSmallIntegerField("халявность", null=True, blank=True, validators=SCORE_VALIDATORS)

    liked_users = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="liked_teacher_reviews", blank=True)
    disliked_users = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="disliked_teacher_reviews", blank=True)

    created = models.DateTimeField("создан", default=timezone.now)

    class Meta:
        verbose_name = "отзыв"
        verbose_name_plural = "отзывы"
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(fields=["author", "teacher"], name="unique_review_per_author_teacher"),
        ]

    def __str__(self):
        return f"{self.author} → {self.teacher}"

    def is_detailed(self):
        """Есть что читать — текст или картинка. Такие отзывы видны всегда и им ставят
        лайки; голые оценки сворачиваются под кнопку."""
        return bool(self.text or self.image)

    def overall(self):
        rated = [getattr(self, f) for f in SCORE_LABELS if getattr(self, f) is not None]
        return round(sum(rated) / len(rated), 2) if rated else None

    def score_items(self):
        return [(label, getattr(self, f)) for f, label in SCORE_LABELS.items() if getattr(self, f) is not None]
