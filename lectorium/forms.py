from django import forms

from core.widgets import AccentSelect, AccentSelectMultiple

from .models import Lecture, Playlist


class PlaylistForm(forms.ModelForm):
    class Meta:
        model = Playlist
        fields = ("title", "subject", "terms", "teachers", "synopsis", "year")
        widgets = {
            # Предметов под восемьдесят — с поиском; семестров дюжина — без.
            "subject": AccentSelect(search=True),
            "terms": AccentSelectMultiple,
            "teachers": AccentSelectMultiple(search=True),
            "synopsis": forms.Textarea(attrs={"rows": 3}),
        }


class LectureForm(forms.ModelForm):
    """Название записи; файл уже в хранилище, в форме — только подписанный токен."""

    class Meta:
        model = Lecture
        fields = ("title",)
