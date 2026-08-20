from django import forms

from attachments import uploads
from attachments.models import human_size

from .models import SCORE_LABELS, Review

SCORE_FIELDS = tuple(SCORE_LABELS)


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = SCORE_FIELDS + ("text", "image", "hide_author")
        labels = {**SCORE_LABELS, "text": "Текст отзыва", "hide_author": "Оставить анонимно"}
        widgets = {"text": forms.Textarea(attrs={"rows": 4})}

    def score_fields(self):
        return [self[name] for name in SCORE_FIELDS]

    def clean_image(self):
        # Тот же потолок, что и у картинки комментария: она едет обычным multipart
        # и держит воркер, пока едет.
        image = self.cleaned_data["image"]
        if image and image.size > uploads.MAX_IMAGE_SIZE:
            raise forms.ValidationError(f"Картинка больше {human_size(uploads.MAX_IMAGE_SIZE)}")
        return image

    def clean(self):
        data = super().clean()
        empty = not data.get("text") and not data.get("image")
        if empty and not any(data.get(name) for name in SCORE_FIELDS):
            raise forms.ValidationError("Поставь хотя бы одну оценку или напиши текст отзыва.")
        return data
