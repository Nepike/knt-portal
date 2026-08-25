from django import forms

from core.widgets import AccentSelect, AccentSelectMultiple

from .models import Lecture, Playlist


class PlaylistForm(forms.ModelForm):
    class Meta:
        model = Playlist
        fields = ("title", "subject", "terms", "teachers", "synopsis", "year")
        widgets = {
            "subject": AccentSelect,
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
