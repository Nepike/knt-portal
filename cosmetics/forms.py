from django import forms

from .models import CosmeticItem
from .specs import validate, validate_video


class CosmeticItemForm(forms.ModelForm):
    """Форма админки: приёмка вещи по спеке.

    Проверяем только что ЗАГРУЖЕНО сейчас: у сохранённой вещи в поле лежит уже принятый
    файл, и гонять его через спеку заново незачем — правка названия не должна упираться
    в то, что рамка со старого сайта на два пикселя не квадратная.
    """

    class Meta:
        model = CosmeticItem
        # Источник проставляют команды переноса, дата ставится сама — руками их не правят
        # (в админке они и так только на просмотр).
        exclude = ("source", "created")

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        if not kind:
            return data
        if self.files.get("image") and data.get("image"):
            validate(kind, data["image"])
        if self.files.get("video") and data.get("video"):
            validate_video(kind, data["video"])
        return data
