from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.utils import timezone

from attachments.storage import media_storage, random_key


def photo_upload_to(instance, filename):
    return random_key("avatars", filename)


class UserManager(BaseUserManager):
    def create_user(self, email, name, surname, patronymic="", password=None, **extra):
        if not email:
            raise ValueError("У пользователя должен быть email")
        user = self.model(
            email=self.normalize_email(email),
            name=name,
            surname=surname,
            patronymic=patronymic,
            **extra,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, name, surname, patronymic="", password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("must_change_password", False)
        return self.create_user(email, name, surname, patronymic, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField("email", max_length=254, unique=True)
    name = models.CharField("имя", max_length=50)
    surname = models.CharField("фамилия", max_length=50)
    patronymic = models.CharField("отчество", max_length=50, blank=True)

    is_active = models.BooleanField("активен", default=True)
    is_staff = models.BooleanField("модератор", default=False)
    must_change_password = models.BooleanField("сменить пароль при входе", default=True)

    photo = models.ImageField("фото", upload_to=photo_upload_to, storage=media_storage, null=True, blank=True)
    birthday = models.DateField("дата рождения", null=True, blank=True)
    phone = models.CharField("телефон", max_length=30, blank=True)
    vk_page = models.CharField("VK (без https://vk.com/)", max_length=50, blank=True)
    tg_page = models.CharField("TG (без https://t.me/)", max_length=50, blank=True)

    mailing_allowed = models.BooleanField("согласие на рассылку", default=True)
    note = models.TextField("заметка", blank=True, default="")

    team = models.ForeignKey("core.Team", verbose_name="группа", on_delete=models.PROTECT, null=True, blank=True, related_name="members",)
    date_joined = models.DateTimeField("дата регистрации", default=timezone.now)

    # TODO (M5): баланс/валюта -> приложение economy (источник истины — BalanceLog)
    # TODO (M5): уровни, опыт, значки -> приложение profiles
    # TODO (M1): назначение группы по умолчанию + kick()/ban() (вместе с сессиями)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name", "surname"]

    class Meta:
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"
        ordering = ["id"]

    def __str__(self):
        return f"{self.name} {self.surname} ({self.email})"
