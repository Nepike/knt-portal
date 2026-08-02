from django.conf import settings
from django.db import models
from django.db.models import Count, F, Q, Value
from django.db.models.functions import Coalesce
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

REACTIONS = ["👍", "❤️", "🔥", "😂", "😮", "😢"]


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
        ("team", "учебная группа"),
    )

    kind = models.CharField("тип", max_length=10, choices=KIND_CHOICES, default="dm")
    title = models.CharField("название", max_length=100, blank=True)
    team = models.OneToOneField(
        "core.Team", verbose_name="учебная группа", on_delete=models.CASCADE,
        null=True, blank=True, related_name="chat",
    )
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
            ("curate_team_chats", "Может быть куратором чата учебной группы"),
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

    def other_member(self, user):
        """Собеседник в ЛС — от него берём имя и аватар для шапки и списка."""
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
    # null = системное сообщение («X добавил Y»); SET_NULL, чтобы удаление автора
    # не выбивало дыры в чужой переписке.
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
    # Пусто, пока контент не менялся. Правка/удаление/реакция ставят метку,
    # и поллинг разошлёт пузырь oob-заменой (см. messages_new).
    updated = models.DateTimeField("изменение контента", null=True, blank=True)

    class Meta:
        verbose_name = "сообщение"
        verbose_name_plural = "сообщения"
        # Сортировка по id, а не created: у одновременных сообщений таймстемпы
        # совпадают и порядок «плывёт». id — он же курсор для догрузки новых.
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


# Чат учебной группы заводится сам: студента вносим в чат его группы, из чата
# прежней группы убираем (перевёлся — переехал вместе с ним).
@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def sync_team_chat(sender, instance, update_fields=None, **kwargs):
    if update_fields and "team" not in update_fields:
        return  # частый случай — обновление last_login при входе
    # Кураторы состоят в чужих team-чатах намеренно — их membership'ы не трогаем.
    if not instance.has_perm("chats.curate_team_chats"):
        Membership.objects.filter(user=instance, chat__kind="team").exclude(chat__team_id=instance.team_id).delete()
    if instance.team_id:
        chat, _ = Chat.objects.get_or_create(
            team_id=instance.team_id,
            defaults={"kind": "team", "title": str(instance.team)},
        )
        Membership.objects.get_or_create(chat=chat, user=instance)
