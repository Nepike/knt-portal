"""Подбор по семестру, предмету и преподавателю — общий для материалов и лектория
(у лектория ещё строка поиска). У библиотеки свой: общего слишком мало.
"""

from urllib.parse import urlencode

from django import forms
from django.db.models import Q

from teachers.models import Teacher

from .models import Subject, Term
from .widgets import AccentSelect

# Поле фильтра → связь записи; у материала и курса имена совпадают.
FIELDS = {"term": "terms", "subject": "subject", "teacher": "teachers"}
# Строка поиска — не в форме фильтров: у неё своя разметка.
SEARCH = "q"
# Больше слов не ищем: каждое — отдельный LIKE.
MAX_WORDS = 6


class FilterForm(forms.Form):

    subject = forms.ModelChoiceField(
        label="Предмет", queryset=Subject.objects.all(), required=False, widget=AccentSelect(search=True),
    )
    term = forms.ModelChoiceField(
        label="Семестр", queryset=Term.objects.all(), required=False, widget=AccentSelect(),
    )
    teacher = forms.ModelChoiceField(
        label="Преподаватель", queryset=Teacher.objects.all(), required=False, widget=AccentSelect(search=True),
    )


def chosen(form):
    """Что человек выбрал: {поле: объект}. Пусто, если форма не сошлась."""
    return {name: form.cleaned_data[name] for name in FIELDS} if form.is_valid() else {}


def apply(items, picked):
    for name, lookup in FIELDS.items():
        if picked.get(name):
            items = items.filter(**{lookup: picked[name]})
    return items


def asked(params):
    return params.get(SEARCH, "").strip()


def search(items, text):
    """Каждое слово строки — в названии, в любом порядке: «матан лекции» найдёт «Лекции по матану»."""
    for word in text.split()[:MAX_WORDS]:
        items = items.filter(title__icontains=word)
    return items


def query(params, search=False):
    """Непустой подбор строкой запроса — для адреса списка и ссылки «назад» со страницы
    записи. Без строки поиска (search=False) чужой `q` в ссылки не переносим."""
    names = (SEARCH, *FIELDS) if search else FIELDS
    return urlencode({name: value for name in names if (value := params.get(name, "").strip())})


def url(request, search=False):
    """Адрес с текущими фильтрами: ссылку можно переслать, а F5 не сбросит подбор."""
    found = query(request.GET, search)
    return f"{request.path}?{found}" if found else request.path


def narrow(form, base, picked):
    """Сузить варианты каждого селекта до встречающихся у записей, отобранных ОСТАЛЬНЫМИ
    фильтрами. Свой фильтр не учитываем — иначе его не сменить; выбранное добавляем явно.
    Сужается только отрисовка: форма проверена по полным наборам.
    """
    for name, lookup in FIELDS.items():
        rest = base
        for other, other_lookup in FIELDS.items():
            if other != name and picked.get(other):
                rest = rest.filter(**{other_lookup: picked[other]})
        found = Q(pk__in=rest.values(lookup))
        if picked.get(name):
            found |= Q(pk=picked[name].pk)
        field = form.fields[name]
        field.queryset = field.queryset.filter(found)
