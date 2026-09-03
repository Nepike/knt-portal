from base64 import b64decode
from binascii import Error as BadBase64
from io import BytesIO

from django import forms
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm
from django.contrib.auth.forms import UserCreationForm as BaseUserCreationForm
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from PIL import Image as PilImage

from core.widgets import AccentSelect, AccentSelectMultiple

from .models import User

# Сторона готовой миниатюры. Крупнее её не показывают нигде, а в бакете это ~50 КБ.
AVATAR_PX = 512
# Длина data-URL в POST. Должна оставаться ниже DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 МБ):
# за ним Django отвечает голым 400, а не ошибкой в форме. Столько же знает и браузер —
# значение уезжает в avatarPick, чтобы он успел пережать картинку сам.
MAX_AVATAR_DATA = 2 * 1024 * 1024
# Гифка едет файлом и целиком (см. ProfileForm.photo_file), поэтому лимит ей нужен свой.
MAX_GIF = 3 * 1024 * 1024
DROP = "clear"


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


def _thumbnail(raw):
    """Квадрат AVATAR_PX из присланных байтов.

    Перерисовываем своим Pillow, а не сохраняем как есть: пришла строка из браузера,
    и подделать её ничего не стоит. Заодно отваливается всё, что не картинка, и уходит
    exif — в нём у телефонных снимков лежит геометка съёмки.

    Прозрачность бережём: на аватарах её любят, а плоский белый квадрат в тёмной теме
    выглядел бы заплаткой.
    """
    try:
        image = PilImage.open(BytesIO(raw))
        image.load()
    except (OSError, ValueError, PilImage.DecompressionBombError):
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
    """Аватар приезжает готовым квадратом в data-URL, а не файлом.

    Миниатюру человек выбирает сам: снимок с телефона бывает 4000×3000 и вертикальный,
    и в квадрат аватара от него попадал бы случайный кусок. Рамку кадра двигают на
    клиенте (avatarPick в components.js), сюда приходит уже вырезанное.

    Три состояния: пусто — не трогать, «clear» — снять, data-URL — заменить.
    """

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
    """Из «https://t.me/ivan», «@ivan» и «ivan» одинаково получается «ivan».
    Люди приносят ссылку целиком, а в шаблоне она приклеивается к адресу второй раз.

    Наружу — ради ведомости (`roster`): в форме на телеграм отвечают так же вольно."""
    tail = value.strip().rpartition(f"{host}/")[2]
    return tail.split("?")[0].strip("/@")


class ProfileForm(forms.ModelForm):
    """Что человек меняет о себе сам.

    ФИО, группы и почты тут нет намеренно: имя стоит подписью под каждым материалом
    и отзывом, и свободное переименование на закрытом сайте — готовый способ выдать
    себя за другого. Кому надо поправить — через администратора.
    """

    photo = AvatarField(required=False)
    # Гифки исключение: канвас забирает из них один кадр, и анимация пропала бы.
    # Такие едут файлом и без кадрирования — тем же центральным квадратом, каким
    # их и так показывает object-cover.
    photo_file = forms.ImageField(required=False)

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
        """Одна строка значит одна: переносы и лишние пробелы схлопываем сами, иначе
        человек растянет подпись на полстраницы и сломает шапку профиля соседям в списках."""
        return " ".join(self.cleaned_data["status"].split())

    def clean_photo_file(self):
        upload = self.cleaned_data["photo_file"]
        if not upload:
            return None
        # Целиком, без пережатия, пропускаем ровно гифки — остальное браузер обязан
        # был порезать сам. Иначе этим полем можно было бы залить что угодно любого размера.
        if upload.image.format != "GIF":
            raise ValidationError("Файлом принимаем только гифки")
        if upload.size > MAX_GIF:
            raise ValidationError(f"Гифка тяжелее {MAX_GIF // 1024 // 1024} МБ")
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
    """Подбор для списка людей. Поисковая строка живёт отдельно в шаблоне — у неё
    своя разметка с лупой, как в библиотеке.

    Форма только рисует: значение вьюха разбирает сама и отдаёт сюда уже разобранным
    (`initial`). Так селект всегда показывает ровно то, что применено, — а не то,
    что прислали.
    """

    course = forms.ChoiceField(label="Курс", required=False, widget=AccentSelect())

    def __init__(self, *args, courses=(), **kwargs):
        super().__init__(*args, **kwargs)
        # Пустой вариант первым — это «любой курс», без него сбросить выбор было бы нечем.
        self.fields["course"].choices = [("", "")] + list(courses)
