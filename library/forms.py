from datetime import date

from django import forms

from core.models import Subject, Term
from core.widgets import AccentSelect, AccentSelectMultiple

from .models import Book


class BookFilterForm(forms.Form):
    """Фильтры списка. Поисковая строка живёт отдельно в шаблоне — у неё своя разметка с лупой."""

    subject = forms.ModelChoiceField(
        label="Предмет", queryset=Subject.objects.all(), required=False, widget=AccentSelect(search=True),
    )
    term = forms.ModelChoiceField(
        label="Семестр", queryset=Term.objects.all(), required=False, widget=AccentSelect(),
    )


class BookForm(forms.ModelForm):
    class Meta:
        model = Book
        fields = ["title", "authors", "year", "subjects", "terms", "hide_uploader"]
        labels = {"title": "Название", "hide_uploader": "Загрузить анонимно"}
        widgets = {
            "subjects": AccentSelectMultiple(search=True),
            "terms": AccentSelectMultiple(),
        }

    def clean_year(self):
        year = self.cleaned_data["year"]
        if year and not 1450 <= year <= date.today().year + 1:
            raise forms.ValidationError("Похоже на опечатку — проверь год издания.")
        return year
