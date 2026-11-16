from django import forms
from django.core.files.uploadedfile import UploadedFile

from attachments import uploads
from core.forms import CappedImageField

from .models import SCORE_LABELS, Review

SCORE_FIELDS = tuple(SCORE_LABELS)


class ReviewForm(forms.ModelForm):
    # Потолок — как у картинки комментария, и так же до Pillow.
    image = CappedImageField(
        label="Картинка", required=False, max_size=uploads.MAX_IMAGE_SIZE,
        error_messages={"invalid_image": uploads.UNREADABLE_PICTURE},
    )

    class Meta:
        model = Review
        fields = SCORE_FIELDS + ("text", "image", "hide_author")
        labels = {**SCORE_LABELS, "text": "Текст отзыва", "hide_author": "Оставить анонимно"}
        widgets = {"text": forms.Textarea(attrs={"rows": 4})}

    def score_fields(self):
        return [self[name] for name in SCORE_FIELDS]

    def clean_image(self):
        image = self.cleaned_data["image"]
        # Только новую: прежняя — поле записи, её пришлось бы читать из хранилища.
        if not isinstance(image, UploadedFile):
            return image
        if problem := uploads.check_picture(image):
            raise forms.ValidationError(problem)
        # Отзыв бывает анонимным, а снимок с телефона несёт координаты и модель телефона.
        return uploads.scrubbed(image)

    def clean(self):
        data = super().clean()
        empty = not data.get("text") and not data.get("image")
        if empty and not any(data.get(name) for name in SCORE_FIELDS):
            raise forms.ValidationError("Поставь хотя бы одну оценку или напиши текст отзыва.")
        return data
