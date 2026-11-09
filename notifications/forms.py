import re

from django import forms

from core.models import Team
from core.widgets import AccentSelectMultiple

from .announcements import course_choices


class AnnouncementForm(forms.Form):
    text = forms.CharField(
        label="Текст", max_length=2000, widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Можно Markdown: **жирный**, [ссылка](https://…). В колокольчике видно начало, целиком — по нажатию.",
    )
    link = forms.CharField(
        label="Ссылка", max_length=300, required=False,
        help_text="Необязательно: кнопка «Перейти» под объявлением — адрес на сайте (/materials/…) или полный, с https://",
    )
    courses = forms.MultipleChoiceField(label="Курсы", required=False, widget=AccentSelectMultiple)
    teams = forms.ModelMultipleChoiceField(
        label="Группы", queryset=Team.objects.all(), required=False, widget=AccentSelectMultiple(search=True),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Курсы — по нынешнему составу групп, как на странице студентов: «6 курс», за которым
        # никого нет, предлагать незачем.
        self.fields["courses"].choices = course_choices()

    def clean_link(self):
        link = self.cleaned_data["link"].strip()
        # «//чужой.сайт» браузер понимает как адрес чужого сайта, а не страницы нашего
        if link and not (re.match(r"https?://\S+$", link) or re.match(r"/(?!/)\S*$", link)):
            raise forms.ValidationError("Ссылка — адрес на сайте (/materials/…) или полный, с https://")
        return link
