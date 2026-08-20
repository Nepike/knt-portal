from django import forms

from attachments.models import human_size

from .widgets import AccentSelect, AccentSelectMultiple

# Картинка обращения едет в телеграм ЧЕРЕЗ ОЧЕРЕДЬ, байтами в самой задаче: на сайте
# у неё нет владельца, и в хранилище она осталась бы сиротой. Отсюда и потолок скромнее,
# чем у картинок галереи, — это всё-таки сообщение в брокере, а не файл на диске.
MAX_SUPPORT_IMAGE = 5 * 1024 * 1024

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


class SupportForm(forms.Form):
    """Обращение в поддержку. Ничего не хранит: уходит сообщением в телеграм.

    Базы под обращения нет намеренно — отвечает на них живой человек в чате, и запись
    в таблице, куда никто не заглядывает, только создавала бы вид работающей очереди.

    Темы общие и от разделов не зависят: страница переживёт бету, а список разделов
    к тому времени сменится не раз.
    """

    TOPICS = [
        ("broken", "Что-то не работает"),
        ("content", "Ошибка в содержимом"),
        ("account", "Аккаунт и доступ"),
        ("idea", "Предложение"),
        ("other", "Другое"),
    ]

    topic = forms.ChoiceField(label="Тема", choices=TOPICS, initial="broken", widget=AccentSelect)
    text = forms.CharField(label="Сообщение", max_length=2000, widget=forms.Textarea(attrs={"rows": 8}))
    image = forms.ImageField(label="Картинка", required=False)
    contact = forms.CharField(label="Почта или телеграм для ответа", max_length=100)

    def __init__(self, *args, known=False, **kwargs):
        """`known` — человек вошёл на сайт. Тогда контакт не спрашиваем вовсе: в чат уезжает
        ссылка на его профиль, а там и телеграм, и ВК. Не вошёл (пишет со страницы входа —
        это как раз частый случай) — без контакта ответить будет некуда."""
        super().__init__(*args, **kwargs)
        if known:
            del self.fields["contact"]

    def clean_image(self):
        image = self.cleaned_data["image"]
        if image and image.size > MAX_SUPPORT_IMAGE:
            raise forms.ValidationError(f"Картинка больше {human_size(MAX_SUPPORT_IMAGE)}")
        return image


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

