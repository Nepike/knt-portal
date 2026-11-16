import time

from django import forms
from django.core import signing

from attachments.models import human_size

from .widgets import AccentSelect, AccentSelectMultiple

# Картинка к обращению едет в телеграм байтами в задаче очереди — потолок скромнее,
# чем у галереи.
MAX_SUPPORT_IMAGE = 5 * 1024 * 1024


class CappedImageField(forms.ImageField):
    """Картинка, которую меряют ДО разбора Pillow (обычный ImageField открывает её ещё
    в to_python). Для форм, открытых гостям: разбор кривого файла держит воркер минутами.
    """

    def __init__(self, *, max_size, **kwargs):
        self.max_size = max_size
        super().__init__(**kwargs)

    def to_python(self, data):
        if data and getattr(data, "size", 0) > self.max_size:
            raise forms.ValidationError(f"Картинка больше {human_size(self.max_size)}", code="too_big")
        return super().to_python(data)


SUBJECTS = [
    ("1", "Алгебра"), ("2", "Геометрия"), ("3", "Математический анализ"),
    ("4", "Физика"), ("5", "Программирование"), ("6", "Английский язык"),
    ("14", "Физика"), ("15", "Программирование"), ("16", "Английский язык"),
    ("24", "Физика"), ("25", "Программирование"), ("26", "Английский язык"),
]
TEACHERS = [
    ("1", "Петров А.С."), ("2", "Сидорова Е.В."), ("3", "Иванов И.И."),
    ("4", "Кузнецова О.П."), ("5", "Смирнов Д.А."),
    ("11", "Петров А.С."), ("12", "Сидорова Е.В."), ("13", "Иванов И.И."),
    ("14", "Кузнецова О.П."), ("15", "Смирнов Д.А."),
]
PLANS = [("free", "Бесплатно"), ("pro", "Про"), ("team", "Командный")]


class StampedForm(forms.Form):
    """Форма знает, сколько её заполняли (признак для core/spam.py). Время открытия —
    в скрытом поле с подписью сервера, без скриптов. Форма с ошибкой возвращает ту же метку.
    """

    SALT = "core.forms.started"

    started = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["started"].initial = signing.dumps(time.time(), salt=self.SALT)

    def seconds(self):
        """Секунд от открытия до отправки; None — метки нет или она чужая."""
        try:
            started = float(signing.loads(self.data.get("started") or "", salt=self.SALT))
        except (signing.BadSignature, TypeError, ValueError):
            return None
        return max(0, int(time.time() - started))


class SupportForm(StampedForm):
    """Обращение в поддержку: в телеграм и в `core.Inquiry`. Темы не привязаны к разделам."""

    TOPICS = [
        ("broken", "Что-то не работает"),
        ("content", "Ошибка в содержимом"),
        ("account", "Аккаунт и доступ"),
        ("idea", "Предложение"),
        ("other", "Другое"),
    ]

    topic = forms.ChoiceField(label="Тема", choices=TOPICS, initial="broken", widget=AccentSelect)
    text = forms.CharField(label="Сообщение", max_length=2000, widget=forms.Textarea(attrs={"rows": 8}))
    image = CappedImageField(label="Картинка", required=False, max_size=MAX_SUPPORT_IMAGE)
    contact = forms.CharField(label="Почта или телеграм для ответа", max_length=100)

    def __init__(self, *args, known=False, **kwargs):
        """`known` — человек вошёл: контакт не спрашиваем, в чат уходит ссылка на профиль."""
        super().__init__(*args, **kwargs)
        if known:
            del self.fields["contact"]


class FeedbackForm(StampedForm):
    """Вопрос со страницы контактов: как SupportForm, но в другой чат. Темы и капчи нет
    намеренно; гостей проверяет core/spam.py."""

    text = forms.CharField(label="Вопрос", max_length=2000, widget=forms.Textarea(attrs={"rows": 4}))
    image = CappedImageField(label="Картинка", required=False, max_size=MAX_SUPPORT_IMAGE)
    name = forms.CharField(label="Как вас зовут", max_length=100)
    contact = forms.CharField(label="Почта, телеграм или ВК для ответа", max_length=100)

    def __init__(self, *args, known=False, **kwargs):
        """`known` — человек вошёл: имя и контакт есть в профиле, ссылка на него уходит в чат."""
        super().__init__(*args, **kwargs)
        if known:
            del self.fields["name"]
            del self.fields["contact"]


class DemoForm(forms.Form):
    subject_plain = forms.ChoiceField(label="Предмет", choices=SUBJECTS, required=False, widget=AccentSelect)
    subject_search = forms.ChoiceField(label="Предмет", choices=SUBJECTS, required=False, widget=AccentSelect(search=True))
    teachers_plain = forms.MultipleChoiceField(label="Преподаватели", choices=TEACHERS, required=False, widget=AccentSelectMultiple)
    teachers_search = forms.MultipleChoiceField(label="Преподаватели", choices=TEACHERS, required=False, widget=AccentSelectMultiple(search=True))

    title = forms.CharField(label="Заголовок", required=True)
    email = forms.EmailField(label="Email", required=False)
    count = forms.IntegerField(label="Количество", required=False)
    event_date = forms.DateField(label="Дата события", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    bio = forms.CharField(label="О себе", required=False, widget=forms.Textarea)

    plan = forms.ChoiceField(label="Тариф", choices=PLANS, required=True, widget=forms.RadioSelect)
    anon = forms.BooleanField(label="Анонимно", required=False)
    notify = forms.BooleanField(label="Уведомления", required=True)

