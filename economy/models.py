from django.conf import settings
from django.db import models
from django.utils import timezone


class Wallet(models.Model):
    """Кошелёк отдельной строкой, а не полем у пользователя.

    Причина практическая: в нескольких местах по коду живёт обычный user.save() —
    он пишет все поля разом (так, например, chats заводит курсовые чаты). Будь баланс
    полем User, такое сохранение затирало бы списание, случившееся секундой раньше.
    Заодно и блокировка получается узкой: вход в систему не ждёт чужую покупку.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="владелец",
        on_delete=models.CASCADE, related_name="wallet",
    )
    # Кэш: истина — сумма по журналу. Расхождение показывает и чинит recount_balances.
    balance = models.PositiveIntegerField("баланс", default=0)

    class Meta:
        verbose_name = "кошелёк"
        verbose_name_plural = "кошельки"
        ordering = ["-balance"]

    def __str__(self):
        return f"{self.user}: {self.balance}"


class BalanceLog(models.Model):
    """Журнал операций — источник истины по валюте.

    Причины пополняются по мере появления функций: сейчас реальны выдача вручную
    и смена цвета на Стене, дальше сюда придут начисления за вклад.
    """

    class Reason(models.TextChoices):
        MANUAL = "manual", "начисление вручную"
        WALL_REROLL = "wall_reroll", "смена цвета на Стене"

    wallet = models.ForeignKey(
        Wallet, verbose_name="кошелёк", on_delete=models.CASCADE, related_name="entries",
    )
    amount = models.IntegerField("сумма")  # плюс — начисление, минус — трата
    reason = models.CharField("причина", max_length=30, choices=Reason.choices)
    note = models.CharField("примечание", max_length=200, blank=True)
    # Баланс на момент операции: иначе в истории не показать, из чего он сложился,
    # а каждую строку пришлось бы досчитывать суммой всей предыдущей ленты.
    balance_after = models.PositiveIntegerField("баланс после")
    created = models.DateTimeField("когда", default=timezone.now)

    class Meta:
        verbose_name = "операция"
        verbose_name_plural = "операции"
        ordering = ["-id"]
        indexes = [models.Index(fields=["wallet", "-id"])]

    def __str__(self):
        return f"{self.amount:+} ({self.get_reason_display()})"
