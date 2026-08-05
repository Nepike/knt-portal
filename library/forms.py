from django import forms

from core.models import Subject, Term
from core.widgets import AccentSelect


class BookFilterForm(forms.Form):
    """Фильтры списка. Поисковая строка живёт отдельно в шаблоне — у неё своя разметка с лупой."""

    subject = forms.ModelChoiceField(
        label="Предмет", queryset=Subject.objects.all(), required=False, widget=AccentSelect(search=True),
    )
    term = forms.ModelChoiceField(
        label="Семестр", queryset=Term.objects.all(), required=False, widget=AccentSelect(),
    )
