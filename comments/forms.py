from django import forms
from django.core.files.uploadedfile import UploadedFile

from attachments import uploads
from attachments.models import human_size

from .models import Comment


MAX_TEXT = 4000  # знаков


class CommentForm(forms.ModelForm):
    max_text = MAX_TEXT  # его же берёт maxlength поля ввода (_comment_fields.html)

    class Meta:
        model = Comment
        fields = ["text", "image", "hide_author"]
        labels = {"text": "Комментарий", "hide_author": "Анонимно"}
        error_messages = {"image": {"invalid_image": uploads.UNREADABLE_PICTURE}}

    def clean_text(self):
        # maxlength в поле обходит любой запрос мимо страницы, а текст проходит разметку
        # при каждом показе ленты. Перенос строки браузер считает за один знак, а шлёт двумя.
        text = self.cleaned_data["text"]
        if len(text.replace("\r\n", "\n")) > MAX_TEXT:
            raise forms.ValidationError(f"Комментарий длиннее {MAX_TEXT} знаков")
        return text

    def clean_image(self):
        # Картинка комментария идёт обычным multipart и держит воркер, пока едет,
        # — тот же потолок, что и у галереи.
        image = self.cleaned_data["image"]
        if image and image.size > uploads.MAX_IMAGE_SIZE:
            raise forms.ValidationError(f"Картинка больше {human_size(uploads.MAX_IMAGE_SIZE)}")
        # Только новую: прежняя картинка — это поле записи, и читать её пришлось бы из хранилища.
        if isinstance(image, UploadedFile) and (problem := uploads.check_picture(image)):
            raise forms.ValidationError(problem)
        return image

    def clean(self):
        data = super().clean()
        if not data.get("text", "").strip() and not data.get("image"):
            raise forms.ValidationError("Пустой комментарий отправлять некуда.")
        return data
