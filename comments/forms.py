from django import forms

from attachments import uploads
from attachments.models import human_size

from .models import Comment


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ["text", "image", "hide_author"]
        labels = {"text": "Комментарий", "hide_author": "Анонимно"}

    def clean_image(self):
        # Картинка комментария идёт обычным multipart и держит воркер, пока едет,
        # — тот же потолок, что и у галереи.
        image = self.cleaned_data["image"]
        if image and image.size > uploads.MAX_IMAGE_SIZE:
            raise forms.ValidationError(f"Картинка больше {human_size(uploads.MAX_IMAGE_SIZE)}")
        return image

    def clean(self):
        data = super().clean()
        if not data.get("text", "").strip() and not data.get("image"):
            raise forms.ValidationError("Пустой комментарий отправлять некуда.")
        return data
