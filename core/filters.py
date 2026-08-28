"""Подбор по семестру, предмету и преподавателю — общий для материалов и лектория.

Поля у них одинаковые (предмет ключом, преподаватели и семестры — многие-ко-многим),
и подбор устроен одинаково, вплоть до сужения вариантов. Двум копиям такого кода
разъехаться было бы делом времени: правку в одной легко не заметить во второй.

Библиотека сюда не входит: там свои поиск по названию и сортировка, и общего остаётся
слишком мало, чтобы это склеивать.
"""

from urllib.parse import urlencode

from django import forms
from django.db.models import Q

from teachers.models import Teacher

from .models import Subject, Term
from .widgets import AccentSelect

# Поле фильтра → как оно цепляется к записи. Имена связей у материала и плейлиста
# совпадают, поэтому карта одна на обоих.
FIELDS = {"term": "terms", "subject": "subject", "teacher": "teachers"}


class FilterForm(forms.Form):
    """Фильтры списка. Поиск, где он есть, живёт отдельно — у него своя разметка с лупой."""

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
    """Оставить в выборке только подходящее под выбранное."""
    for name, lookup in FIELDS.items():
        if picked.get(name):
            items = items.filter(**{lookup: picked[name]})
    return items


def query(params):
    """Непустые фильтры строкой запроса. Из неё собирается и адрес списка, и ссылка
    «назад» на странице записи: без неё возврат к списку сбрасывал бы весь подбор."""
    return urlencode({name: value for name in FIELDS if (value := params.get(name))})


def url(request):
    """Адрес с текущими фильтрами: ссылку можно переслать, а F5 не сбросит подбор."""
    found = query(request.GET)
    return f"{request.path}?{found}" if found else request.path


def narrow(form, base, picked):
    """Оставить в каждом селекте только то, что вообще встречается у записей,
    отобранных ОСТАЛЬНЫМИ фильтрами: выбрал семестр — предметы сузились до его предметов.

    Свой фильтр в расчёт не берём: иначе в списке осталось бы одно уже выбранное значение
    и сменить его было бы нечем. Выбранное на всякий случай добавляем к списку явно —
    семестр могли выбрать после предмета, которого в нём нет, и тогда своего же значения
    в списке не оказалось бы. Формы это не касается: она уже проверена по полным наборам,
    сужаем только то, что рисуется.
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
