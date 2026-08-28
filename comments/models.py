"""Обсуждение — одно на весь сайт.

Комментарий раньше жил в `materials` и был прибит внешним ключом к материалу. Под
лекциями обсуждение нужно ровно такое же: ветки, лайки, картинка, анонимность, — а две
почти одинаковые модели разъехались бы на первой же правке, которую внесли в одну из них.

Владелец — отдельный ключ на каждый вид, как у `attachments.File` (материал или книга).
Общего предка через `GenericForeignKey` не берём по той же причине, что и там: он теряет
целостность на уровне базы, а тут удаление владельца обязано уносить его обсуждение.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from attachments.storage import media_storage, random_key


def comment_image_to(instance, filename):
    return random_key("comments", filename)


class Comment(models.Model):
    material = models.ForeignKey(
        "materials.Material", verbose_name="материал", on_delete=models.CASCADE,
        related_name="comments", null=True, blank=True,
    )
    lecture = models.ForeignKey(
        "lectorium.Lecture", verbose_name="лекция", on_delete=models.CASCADE,
        related_name="comments", null=True, blank=True,
    )

    parent = models.ForeignKey(
        "self", verbose_name="ответ на", on_delete=models.CASCADE,
        null=True, blank=True, related_name="replies",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="автор", on_delete=models.CASCADE,
        related_name="comments",
    )
    hide_author = models.BooleanField("анонимно", default=False)

    text = models.TextField("текст", blank=True)
    image = models.ImageField(
        "изображение", upload_to=comment_image_to, storage=media_storage, null=True, blank=True,
    )

    liked_users = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="liked_comments", blank=True)
    disliked_users = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="disliked_comments", blank=True)

    created = models.DateTimeField("создан", default=timezone.now)

    class Meta:
        verbose_name = "комментарий"
        verbose_name_plural = "комментарии"
        ordering = ["created"]

    def __str__(self):
        return f"{self.author} → {self.owner}"

    @property
    def owner(self):
        """Материал или лекция — то, под чем висит это обсуждение."""
        return self.material or self.lecture

    @property
    def kind(self):
        """Вид владельца строкой — им же адресуются ручки (`comments/urls.py`)."""
        return "material" if self.material_id else "lecture"

    def clean(self):
        if not self.material_id and not self.lecture_id:
            raise ValidationError("Комментарий должен быть привязан к материалу или лекции.")
        if self.material_id and self.lecture_id:
            raise ValidationError("Комментарий не может висеть и под материалом, и под лекцией.")
