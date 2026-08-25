from django import forms

from .models import CosmeticItem
from .specs import validate, validate_video

FILES = ("image", "video")


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

        # Прежние блобы: `clean` идёт ДО того, как форма перепишет поля у instance,
        # поэтому здесь ещё видно, что лежало в них раньше. Ссылку на них после замены
        # взять будет негде, а в бакете они останутся сиротами — снимаем в save().
        # `data[name] is False` — это снятая галочка «очистить» у файлового поля.
        self._stale = []
        for name in FILES:
            old = getattr(self.instance, name, None)
            if old and (self.files.get(name) or data.get(name) is False):
                self._stale.append((old.storage, old.name))

        if self.files.get("image") and data.get("image"):
            validate(kind, data["image"])
        if self.files.get("video") and data.get("video"):
            validate_video(kind, data["video"])
        return data

    def save(self, commit=True):
        item = super().save(commit=commit)
        # Только при commit: без него запись ещё не сохранена, и старый файл может
        # оказаться единственным.
        if commit:
            for storage, name in getattr(self, "_stale", ()):
                storage.delete(name)
        return item
