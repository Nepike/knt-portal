from django.conf import settings
from django.db import models
from django.utils import timezone


class Wallet(models.Model):
    """Кошелёк отдельной строкой: user.save() пишет все поля разом и затирал бы списание.
    Заодно блокировка узкая."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="владелец",
        on_delete=models.CASCADE, related_name="wallet",
    )
    # Кэш: истина — сумма по журналу (recount_balances). Со знаком: в минус уводит возврат
    # награды за удалённое (rewards.remove); покупку не пускает services.spend.
    balance = models.IntegerField("баланс", default=0)

    class Meta:
        verbose_name = "кошелёк"
        verbose_name_plural = "кошельки"
        ordering = ["-balance"]

    def __str__(self):
        return f"{self.user}: {self.balance}"


class BalanceLog(models.Model):
    """Журнал операций — источник истины. Одно правило награды — одна причина."""

    class Reason(models.TextChoices):
        MANUAL = "manual", "начисление вручную"
        WELCOME = "welcome", "стартовые"
        MATERIAL = "material", "материалы"
        BOOK = "book", "книги"
        PLAYLIST = "playlist", "курсы лекций"
        REVIEW = "review", "отзывы о преподавателях"
        LIKES = "likes", "лайки на твоих отзывах и комментариях"
        DOWNLOAD = "download", "скачивания твоих файлов"
        WALL = "wall", "пиксели на Стене"
        MODERATION = "moderation", "проверка чужих работ"
        PURCHASE = "purchase", "покупки в магазине"
        CASE = "case", "кейсы"
        QUEST = "quest", "ключ от взлома"

    # Что можно записать руками: список разрешённого — новая причина по умолчанию машинная.
    # Награду руками нельзя (строка без ключа зачлась бы выплаченной), покупку пишет магазин.
    BY_HAND = frozenset({Reason.MANUAL})
    # Награды, возвращаемые при удалении автором (rewards.remove). Такой минус — не трата:
    # он идёт во вклад.
    TAKEN_BACK = frozenset({Reason.MATERIAL, Reason.BOOK, Reason.PLAYLIST, Reason.REVIEW, Reason.LIKES})

    wallet = models.ForeignKey(
        Wallet, verbose_name="кошелёк", on_delete=models.CASCADE, related_name="entries",
    )
    amount = models.IntegerField("сумма")  # плюс — начисление, минус — трата или возврат
    reason = models.CharField("причина", max_length=30, choices=Reason.choices)
    # За что заплатили: «material:317», «likes:r42» — по ключу rewards.py знает, что оплачено.
    key = models.CharField("за что", max_length=64, blank=True)
    note = models.CharField("примечание", max_length=200, blank=True)
    # Баланс на момент операции — историю не досчитывать.
    balance_after = models.IntegerField("баланс после")
    created = models.DateTimeField("когда", default=timezone.now)
    # Кто провёл операцию руками (админка, Пульс). У машинных — награды, покупки — пусто.
    by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кто провёл", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "операция"
        verbose_name_plural = "операции"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["wallet", "-id"]),
            # По нему rewards считает выплаченное на каждый предмет награды.
            models.Index(fields=["wallet", "reason", "key"]),
        ]

    def __str__(self):
        return f"{self.amount:+} ({self.get_reason_display()})"
