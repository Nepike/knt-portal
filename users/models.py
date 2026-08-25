from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.utils import timezone

from attachments.storage import media_storage, random_key


STATUS_MAX = 140


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
    # Одна строка, а не биография: строка встаёт под именем без отдельного блока, влезает
    # в будущую карточку по наведению, и её легко окинуть взглядом при модерации.
    # Полотно текста со ссылками на закрытом сайте курса пришлось бы читать целиком.
    status = models.CharField("статус", max_length=STATUS_MAX, blank=True)
    birthday = models.DateField("дата рождения", null=True, blank=True)
    phone = models.CharField("телефон", max_length=30, blank=True)
    vk_page = models.CharField("VK (без https://vk.com/)", max_length=50, blank=True)
    tg_page = models.CharField("TG (без https://t.me/)", max_length=50, blank=True)

    mailing_allowed = models.BooleanField("согласие на рассылку", default=True)
    note = models.TextField("заметка", blank=True, default="")

    team = models.ForeignKey("core.Team", verbose_name="группа", on_delete=models.PROTECT, null=True, blank=True, related_name="members",)
    date_joined = models.DateTimeField("дата регистрации", default=timezone.now)

    # Кошелёк — economy.Wallet, надетое и инвентарь — cosmetics.UserItem: полем здесь
    # им делать нечего, обычный user.save() затирал бы чужую запись.
    # TODO (M1): назначение группы по умолчанию, бан аккаунта

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name", "surname"]

    class Meta:
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"
        ordering = ["id"]

    def __str__(self):
        return f"{self.name} {self.surname} ({self.email})"


class UserSession(models.Model):
    """Приписка к сессии: чья она, откуда и когда заходили в последний раз.

    Django хранит сессию зашифрованным блобом, и снаружи не видно даже, чья она, —
    без этой таблицы свои сессии человек не может ни увидеть, ни закрыть.

    Живость определяет НЕ она, а сама django_session: строка привязана каскадом, поэтому
    выход, `clearsessions` и закрытие с этой страницы уносят приписку сами. Отсюда
    зависимость: сессии обязаны лежать в базе — в кэше внешнего ключа не на что вешать.

    Первичный id свой, а не ключ сессии: ключ — пароль на предъявителя, и в разметке
    страницы «мои устройства» ему делать нечего.
    """

    session = models.OneToOneField(
        "sessions.Session", verbose_name="сессия", on_delete=models.CASCADE, related_name="meta",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="пользователь",
        on_delete=models.CASCADE, related_name="sessions",
    )
    ip = models.GenericIPAddressField("адрес", null=True, blank=True)
    agent = models.CharField("браузер", max_length=200, blank=True)
    created = models.DateTimeField("вход", default=timezone.now)
    seen = models.DateTimeField("последний запрос", default=timezone.now)

    class Meta:
        verbose_name = "сессия"
        verbose_name_plural = "сессии"
        ordering = ["-seen"]

    def __str__(self):
        return f"{self.user}: {self.ip or '?'}"

    def fresh(self):
        """Заходили только что. Отметка обновляется раз в несколько минут, точнее «сейчас»
        о ней всё равно не сказать, а «был 0 минут назад» читается как поломка."""
        return timezone.now() - self.seen < timedelta(minutes=10)

    def where(self):
        """Браузер и система человеческими словами.

        Порядок в списках важен: Edge представляется и хромом тоже, хром — ещё и сафари.
        Побеждает первое совпадение, поэтому частные имена стоят раньше общих.
        """
        parts = (_first(self.agent, BROWSERS), _first(self.agent, SYSTEMS))
        if named := " · ".join(part for part in parts if part):
            return named
        # Не узнали. Короткую строку показываем как есть — так себя называет сессия,
        # выданная командой session_for; настоящий User-Agent для этого слишком длинный.
        return self.agent if 0 < len(self.agent) <= 60 else "неизвестное устройство"


BROWSERS = (
    ("YaBrowser", "Яндекс.Браузер"), ("Edg", "Edge"), ("OPR", "Opera"), ("Vivaldi", "Vivaldi"),
    ("Firefox", "Firefox"), ("Chrome", "Chrome"), ("Safari", "Safari"),
)
SYSTEMS = (
    ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
    ("Windows", "Windows"), ("Mac OS", "macOS"), ("Linux", "Linux"),
)


def _first(agent, names):
    return next((name for mark, name in names if mark in agent), "")
