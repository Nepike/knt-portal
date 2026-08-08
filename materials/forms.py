from datetime import date

from django import forms

from attachments import uploads
from attachments.models import human_size
from core.models import Subject, Term
from core.widgets import AccentSelect, AccentSelectMultiple
from teachers.models import Teacher

from .models import Comment, Material


class MaterialFilterForm(forms.Form):
    """Фильтры списка. Поиск живёт отдельно в шаблоне — у него своя разметка с лупой."""

    subject = forms.ModelChoiceField(
        label="Предмет", queryset=Subject.objects.all(), required=False, widget=AccentSelect(search=True),
    )
    term = forms.ModelChoiceField(
        label="Семестр", queryset=Term.objects.all(), required=False, widget=AccentSelect(),
    )
    teacher = forms.ModelChoiceField(
        label="Преподаватель", queryset=Teacher.objects.all(), required=False, widget=AccentSelect(search=True),
    )


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


class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        fields = ["title", "synopsis", "subject", "teachers", "terms", "year", "text", "hide_uploader"]
        labels = {
            "title": "Название",
            "synopsis": "Короткое описание",
            "text": "Текст",
            "hide_uploader": "Загрузить анонимно",
        }
        help_texts = {
            "synopsis": "Одна-две строки — это видно в списке.",
            "text": "Markdown: **жирный**, # заголовок, - список, [ссылка](адрес). Формулы — $E=mc^2$.",
        }
        widgets = {
            "subject": AccentSelect(search=True),
            "teachers": AccentSelectMultiple(search=True),
            "terms": AccentSelectMultiple(),
            "synopsis": forms.Textarea(attrs={"rows": 2}),
            "text": forms.Textarea(attrs={"rows": 14}),
        }

    def clean_year(self):
        year = self.cleaned_data["year"]
        # Год материала — год, когда он был актуален; будущее допускаем на семестр вперёд.
        if year and not 2000 <= year <= date.today().year + 1:
            raise forms.ValidationError("Похоже на опечатку — проверь год.")
        return year
