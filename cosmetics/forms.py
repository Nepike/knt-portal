from django import forms

from .models import Case, CosmeticItem
from .specs import validate, validate_video

FILES = ("image", "video")


class CosmeticItemForm(forms.ModelForm):
    """Форма админки: приёмка вещи по спеке. Проверяем только загруженное сейчас: правка
    названия не должна упираться в старую рамку со старого сайта."""

    class Meta:
        model = CosmeticItem
        # Источник ставят команды переноса, дата — сама.
        exclude = ("source", "created")

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        if not kind:
            return data

        # Прежние блобы: `clean` идёт ДО того, как форма перепишет поля, — тут ещё видно старое.
        # Снимает их админка после записи (CosmeticItemAdmin.save_model). `data[name] is False` —
        # снятая галочка «очистить».
        self.stale = []
        for name in FILES:
            old = getattr(self.instance, name, None)
            if old and (self.files.get(name) or data.get(name) is False):
                self.stale.append((old.storage, old.name))

        if self.files.get("image") and data.get("image"):
            validate(kind, data["image"])
        if self.files.get("video") and data.get("video"):
            validate_video(kind, data["video"])
        return data


class CaseForm(forms.ModelForm):
    """Форма кейса в админке. Пустой кейс не включить: по старой ссылке он взял бы деньги ни за что."""

    class Meta:
        model = Case
        exclude = ("created",)

    def clean(self):
        data = super().clean()
        # Прежняя обложка — как у вещи (CosmeticItemForm): снимается после записи.
        self.stale = []
        old = self.instance.cover if self.instance.pk else None
        if old and (self.files.get("cover") or data.get("cover") is False):
            self.stale.append((old.storage, old.name))
        if data.get("active") and not data.get("items"):
            self.add_error("active", "Сначала выбери вещи: пустой кейс открыть нельзя")
        return data
