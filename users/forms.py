from base64 import b64decode
from binascii import Error as BadBase64
from io import BytesIO

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm
from django.contrib.auth.forms import UserCreationForm as BaseUserCreationForm
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from PIL import Image as PilImage

from core.forms import CappedImageField
from core.widgets import AccentSelect, AccentSelectMultiple

from . import journal
from .models import User

# Сторона готовой миниатюры. Крупнее её не показывают нигде, а в бакете это ~50 КБ.
AVATAR_PX = 512
# Длина data-URL в POST: ниже DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 МБ), иначе Django ответит
# голым 400. То же число получает avatarPick.
MAX_AVATAR_DATA = 2 * 1024 * 1024
# Гифка едет файлом и целиком (см. ProfileForm.photo_file), поэтому лимит ей нужен свой.
MAX_GIF = 3 * 1024 * 1024
# Сторона, которую вообще разбираем. Вдвое больше AVATAR_PX — запрос собран мимо страницы,
# а разжатие стоит памяти (у гифки — браузеру каждого зрителя).
AVATAR_SOURCE_MAX = 2 * AVATAR_PX
DROP = "clear"


class LoginForm(AuthenticationForm):
    """Вход сайта. Отличие одно: остановленному ограничителем говорим правду, а не
    «неверный пароль» — иначе он так и будет подбирать уже верный."""

    def get_invalid_login_error(self):
        if getattr(self.request, journal.STOPPED, False):
            return ValidationError(
                "Слишком много неудачных попыток. Подожди час или восстанови пароль.", code="stopped",
            )
        return super().get_invalid_login_error()


class UserCreationForm(BaseUserCreationForm):
    class Meta:
        model = User
        fields = ("email", "name", "surname")


class UserChangeForm(BaseUserChangeForm):
    class Meta:
        model = User
        fields = "__all__"


def grantable_groups(user):
    """Группы, которые user может выдавать: их права не шире его собственных."""
    if user.is_superuser:
        return Group.objects.all()
    mine = user.get_all_permissions()
    ids = [
        g.id
        for g in Group.objects.prefetch_related("permissions__content_type")
        if {f"{p.content_type.app_label}.{p.codename}" for p in g.permissions.all()} <= mine
    ]
    return Group.objects.filter(id__in=ids)


class RegisterUserForm(forms.ModelForm):
    """Регистрация пользователя: пароль не задаётся — уходит письмо со ссылкой.
    Роли (группы) без выбора = обычный студент."""

    class Meta:
        model = User
        fields = ("name", "surname", "patronymic", "email", "team", "groups")
        labels = {"groups": "Роли"}
        help_texts = {"groups": ""}
        widgets = {"team": AccentSelect(search=True), "groups": AccentSelectMultiple}

    def __init__(self, *args, creator=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["groups"].queryset = grantable_groups(creator) if creator else Group.objects.none()

    def clean_email(self):
        # Сверяем сами: штатная проверка различает регистр, а отказ ограничения базы —
        # ошибка формы, а не поля, и на этой странице её не видно.
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("Человек с такой почтой уже зарегистрирован")
        return email


def _thumbnail(raw):
    """Квадрат AVATAR_PX из присланных байтов — своим Pillow: строку из браузера легко подделать,
    заодно уходит exif. Прозрачность бережём."""
    too_big = ValidationError("Картинка слишком большая — выбери её заново")
    try:
        image = PilImage.open(BytesIO(raw))
    except PilImage.DecompressionBombError:
        raise too_big
    except (OSError, ValueError):
        raise ValidationError("Не получилось прочитать картинку")
    # Размер — из заголовка, до разбора: разбор и есть то, что стоит памяти.
    if max(image.size) > AVATAR_SOURCE_MAX:
        raise too_big
    try:
        image.load()
    except (OSError, ValueError):
        raise ValidationError("Не получилось прочитать картинку")

    alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
    image = image.convert("RGBA" if alpha else "RGB")

    # Квадрат режет браузер, но POST можно прислать и мимо него — приводим сами.
    side = min(image.size)
    left, top = (image.width - side) // 2, (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    if side > AVATAR_PX:
        image = image.resize((AVATAR_PX, AVATAR_PX), PilImage.LANCZOS)

    out = BytesIO()
    if alpha:
        image.save(out, format="PNG", optimize=True)
    else:
        image.save(out, format="JPEG", quality=88, optimize=True)
    return ContentFile(out.getvalue(), name=f"avatar.{'png' if alpha else 'jpg'}")


class AvatarField(forms.CharField):
    """Аватар — готовым квадратом в data-URL (кадр выбирает человек, avatarPick в components.js).
    Пусто — не трогать, «clear» — снять, data-URL — заменить."""

    widget = forms.HiddenInput

    def clean(self, value):
        value = (value or "").strip()
        if value in ("", DROP):
            return value
        if len(value) > MAX_AVATAR_DATA:
            raise ValidationError("Картинка слишком тяжёлая — попробуй другую")
        try:
            raw = b64decode(value.split(",", 1)[1], validate=True)
        except (IndexError, BadBase64):
            raise ValidationError("Не получилось прочитать картинку")
        return _thumbnail(raw)


def handle(value, host):
    """Из «https://t.me/ivan», «@ivan» и «ivan» одинаково получается «ivan». Нужна и ведомости (`roster`)."""
    tail = value.strip().rpartition(f"{host}/")[2]
    return tail.split("?")[0].strip("/@")


class ProfileForm(forms.ModelForm):
    """Что человек меняет о себе сам. ФИО, группы и почты нет намеренно: свободное
    переименование — способ выдать себя за другого."""

    photo = AvatarField(required=False)
    # Гифки — файлом без кадрирования: канвас оставил бы один кадр. Вес — до Pillow.
    photo_file = CappedImageField(required=False, max_size=MAX_GIF)

    class Meta:
        model = User
        fields = ("status", "birthday", "phone", "tg_page", "vk_page", "mailing_allowed")
        labels = {
            "status": "Статус",
            "birthday": "День рождения",
            "phone": "Телефон",
            "tg_page": "Телеграм",
            "vk_page": "ВКонтакте",
            "mailing_allowed": "Получать письма с сайта",
        }
        help_texts = {"status": "Одна строка под именем — видна всем"}
        widgets = {"birthday": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def clean_status(self):
        """Одна строка: переносы и лишние пробелы схлопываем — иначе подпись растянет шапку профиля."""
        return " ".join(self.cleaned_data["status"].split())

    def clean_photo_file(self):
        upload = self.cleaned_data["photo_file"]
        if not upload:
            return None
        # Целиком пропускаем только гифки: остальное режет браузер.
        if upload.image.format != "GIF":
            raise ValidationError("Файлом принимаем только гифки")
        if max(upload.image.size) > AVATAR_SOURCE_MAX:
            raise ValidationError(f"Гифка больше {AVATAR_SOURCE_MAX} точек по стороне — для аватара столько не нужно")
        return upload

    def clean_tg_page(self):
        return handle(self.cleaned_data["tg_page"], "t.me")

    def clean_vk_page(self):
        return handle(self.cleaned_data["vk_page"], "vk.com")

    def save(self, commit=True):
        user = super().save(commit=False)
        photo = self.cleaned_data["photo"]
        if self.cleaned_data["photo_file"]:
            user.photo = self.cleaned_data["photo_file"]
        elif photo == DROP:
            user.photo = None
        elif photo:
            user.photo = photo
        if commit:
            user.save()
        return user


class StudentFilterForm(forms.Form):
    """Подбор для списка людей (поиск — отдельно в шаблоне). Форма только рисует: значение
    разбирает вьюха и отдаёт в `initial` — селект показывает применённое."""

    course = forms.ChoiceField(label="Курс", required=False, widget=AccentSelect())

    def __init__(self, *args, courses=(), **kwargs):
        super().__init__(*args, **kwargs)
        # Пустой вариант первым — это «любой курс», без него сбросить выбор было бы нечем.
        self.fields["course"].choices = [("", "")] + list(courses)
