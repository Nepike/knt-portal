from django import forms

from core.widgets import AccentSelect, AccentSelectMultiple

from .models import Lecture, Playlist


class PlaylistForm(forms.ModelForm):
    class Meta:
        model = Playlist
        fields = ("title", "subject", "terms", "teachers", "synopsis", "year")
        widgets = {
            # С поиском: предметов под восемьдесят, и листать их до нужного — мучение.
            # Семестрам ниже он ни к чему, их дюжина и они по порядку.
            "subject": AccentSelect(search=True),
            "terms": AccentSelectMultiple,
            "teachers": AccentSelectMultiple(search=True),
            "synopsis": forms.Textarea(attrs={"rows": 3}),
        }


class LectureForm(forms.ModelForm):
    """Название записи. Сам файл приезжает отдельно — он уже в хранилище, и в форме
    от него только подписанный токен."""

    class Meta:
        model = Lecture
        fields = ("title",)
