from datetime import date

from django import forms

from core.widgets import AccentSelect, AccentSelectMultiple

from .models import Material


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
