from django.conf import settings
from django.db import models
from django.db.models import Count, F, Q, Value
from django.db.models.functions import Coalesce
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from core.models import Team

# Один список на палитру в UI (reaction_palette) и на проверку входящего эмодзи (message_react).
REACTIONS = ["👍", "❤️", "🔥", "😂", "😮", "😢",]


def unread_total(user):
    """Непрочитанные во всех чатах — для бейджа в меню."""
    return (
        Message.objects.filter(
            chat__memberships__user=user,
            deleted=False,
            id__gt=Coalesce(F("chat__memberships__last_read_id"), Value(0)),
        )
        .exclude(author=user)
        .count()
    )


class Chat(models.Model):
    KIND_CHOICES = (
        ("dm", "личный"),
        ("group", "групповой"),
        ("course", "курс"),
    )

    kind = models.CharField("тип", max_length=10, choices=KIND_CHOICES, default="dm")
    title = models.CharField("название", max_length=100, blank=True)

    # Курс адресуем парой «год поступления + ступень»: номер курса растёт каждый сентябрь,
    # год — нет; ступень нужна, потому что бакалавры и магистры одного набора — разные люди.
    admission_year = models.PositiveSmallIntegerField("год поступления", null=True, blank=True)
    stage = models.CharField("ступень обучения", max_length=20, choices=Team.STAGE_CHOICES, blank=True)

    # Пара id через "_" — БД не даст создать второй диалог тем же двоим.
    dm_key = models.CharField(max_length=32, unique=True, null=True, blank=True, editable=False)

    last_message = models.ForeignKey(
        "Message", verbose_name="последнее сообщение", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    created = models.DateTimeField("создан", default=timezone.now)

    members = models.ManyToManyField(settings.AUTH_USER_MODEL, through="Membership", related_name="chats")

    class Meta:
        verbose_name = "чат"
        verbose_name_plural = "чаты"
        ordering = ["-id"]
        permissions = [
            ("curate_course_chats", "Может быть куратором чата курса"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["admission_year", "stage"], condition=Q(kind="course"), name="unique_course_chat",
            ),
        ]

    def __str__(self):
        return self.title or f"чат #{self.pk}"

    @staticmethod
    def dm_key_for(user, other):
        return "_".join(str(pk) for pk in sorted([user.pk, other.pk]))

    @classmethod
    def get_or_create_dm(cls, user, other):
        chat, created = cls.objects.get_or_create(kind="dm", dm_key=cls.dm_key_for(user, other))
        if created:
            Membership.objects.bulk_create([
                Membership(chat=chat, user=user),
                Membership(chat=chat, user=other),
            ])
        return chat

    @staticmethod
    def course_title(stage, year):
        if year == Team.ALUMNI_YEAR:
            return "Выпускники"  # служебная группа, никакого потока за ней нет
        return f"{dict(Team.STAGE_CHOICES).get(stage, stage)} {year}"

    @classmethod
    def get_or_create_course(cls, team):
        chat, _ = cls.objects.get_or_create(
            kind="course", admission_year=team.year_of_admission, stage=team.stage,
            defaults={"title": cls.course_title(team.stage, team.year_of_admission)},
        )
        return chat

    def is_own_course(self, user):
        """Свой курс покинуть нельзя — в отличие от чужого, где человек куратор."""
        team = user.team
        return bool(team and self.admission_year == team.year_of_admission and self.stage == team.stage)

    def other_member(self, user):
        if self.kind != "dm":
            return None
        return next((m.user for m in self.memberships.all() if m.user_id != user.pk), None)


class MembershipQuerySet(models.QuerySet):
    def with_unread(self, user):
        # last_read пуст у новичка — Coalesce превращает NULL в 0, иначе сравнение
        # с NULL отсечёт все сообщения и непрочитанных «не будет».
        return self.annotate(
            unread=Count(
                "chat__messages",
                filter=Q(chat__messages__id__gt=Coalesce(F("last_read_id"), Value(0)))
                & Q(chat__messages__deleted=False)
                & ~Q(chat__messages__author=user),
            )
        )


class Membership(models.Model):
    chat = models.ForeignKey(Chat, verbose_name="чат", on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="участник", on_delete=models.CASCADE, related_name="chat_memberships")

    last_read = models.ForeignKey(
        "Message", verbose_name="прочитано до", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    is_admin = models.BooleanField("администратор", default=False)
    muted = models.BooleanField("без уведомлений", default=False)  # TODO: задействовать в push/email-уведомлениях
    joined = models.DateTimeField("вступил", default=timezone.now)

    objects = MembershipQuerySet.as_manager()

    class Meta:
        verbose_name = "участник чата"
        verbose_name_plural = "участники чатов"
        constraints = [
            models.UniqueConstraint(fields=["chat", "user"], name="unique_chat_member"),
        ]

    def __str__(self):
        return f"{self.user} в {self.chat}"


class Message(models.Model):
    chat = models.ForeignKey(Chat, verbose_name="чат", on_delete=models.CASCADE, related_name="messages")

    # null = системное сообщение («X добавил Y»); SET_NULL, чтобы удаление автора не выбивало дыры в чужой переписке.
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="автор", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="messages",
    )
    text = models.TextField("текст")
    reply_to = models.ForeignKey(
        "self", verbose_name="ответ на", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="replies",
    )
    # TODO: вложения — nullable FK message на attachments.File/Image (вместе с R2, M2)

    created = models.DateTimeField("создано", default=timezone.now)
    edited = models.DateTimeField("изменено", null=True, blank=True)
    deleted = models.BooleanField("удалено", default=False)
    # Пусто, пока контент не трогали: по метке messages_new отдаёт пузырь oob-заменой.
    updated = models.DateTimeField("изменение контента", null=True, blank=True)

    class Meta:
        verbose_name = "сообщение"
        verbose_name_plural = "сообщения"
        # id, а не created: у одновременных сообщений таймстемпы совпадают и порядок плывёт.
        # Он же курсор — и для догрузки, и для переподключения сокета.
        ordering = ["id"]
        indexes = [models.Index(fields=["chat", "id"])]

    def __str__(self):
        return f"{self.author} в {self.chat}: {self.text[:40]}"


class Reaction(models.Model):
    message = models.ForeignKey(Message, verbose_name="сообщение", on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="пользователь", on_delete=models.CASCADE, related_name="message_reactions")
    emoji = models.CharField("эмодзи", max_length=16)
    created = models.DateTimeField("создана", default=timezone.now)

    class Meta:
        verbose_name = "реакция"
        verbose_name_plural = "реакции"
        constraints = [
            models.UniqueConstraint(fields=["message", "user", "emoji"], name="unique_reaction"),
        ]

    def __str__(self):
        return f"{self.emoji} от {self.user}"


# Чат курса заводится сам: студента вносим в чат его потока, из чужого убираем
# (перевёлся на другой курс — переехал вместе с ним).
@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def sync_course_chat(sender, instance, update_fields=None, **kwargs):
    if update_fields and "team" not in update_fields:
        return  # частый случай — обновление last_login при входе
    team = instance.team
    # Кураторы состоят в чужих курсовых чатах намеренно — их membership'ы не трогаем.
    if not instance.has_perm("chats.curate_course_chats"):
        stale = Membership.objects.filter(user=instance, chat__kind="course")
        if team:
            stale = stale.exclude(chat__admission_year=team.year_of_admission, chat__stage=team.stage)
        stale.delete()
    if team:
        Membership.objects.get_or_create(chat=Chat.get_or_create_course(team), user=instance)
