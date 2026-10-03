from django.conf import settings
from django.db import models
from django.utils import timezone


class Wallet(models.Model):
    """Кошелёк отдельной строкой, а не полем у пользователя.

    В коде хватает мест с обычным user.save() — он пишет все поля разом, и будь баланс
    полем User, такое сохранение затирало бы списание, случившееся секундой раньше.
    Заодно и блокировка узкая: вход в систему не ждёт чужую покупку.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="владелец",
        on_delete=models.CASCADE, related_name="wallet",
    )
    # Кэш: истина — сумма по журналу. Расхождение показывает и чинит recount_balances.
    # Со знаком: в минус уводит возврат награды за то, что автор сам удалил, уже потратив
    # её (rewards.remove). Покупка в минус не уведёт — её не пускает services.spend.
    balance = models.IntegerField("баланс", default=0)

    class Meta:
        verbose_name = "кошелёк"
        verbose_name_plural = "кошельки"
        ordering = ["-balance"]

    def __str__(self):
        return f"{self.user}: {self.balance}"


class BalanceLog(models.Model):
    """Журнал операций — источник истины по валюте.

    Причина — это ещё и ключ пересчёта: по ней rewards.py знает, сколько человеку
    положено ВСЕГО, и дописывает разницу. Поэтому одно правило — одна причина.
    """

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

    # Что можно записать руками из админки. Список разрешённого, а не запрещённого:
    # новая причина по умолчанию окажется машинной, и это правильная сторона ошибки.
    # Награду руками писать нельзя — строка без ключа зачтётся как «уже выплачено»
    # и человек недополучит; покупку пишет магазин вместе с выдачей вещи.
    BY_HAND = frozenset({Reason.MANUAL})
    # Награды, которые возвращаются, когда автор сам удаляет вещь (rewards.remove).
    # Минус с такой причиной — не трата, а отмена заработанного: в «вклад» человека он
    # идёт, в отличие от покупки.
    TAKEN_BACK = frozenset({Reason.MATERIAL, Reason.BOOK, Reason.PLAYLIST, Reason.REVIEW, Reason.LIKES})

    wallet = models.ForeignKey(
        Wallet, verbose_name="кошелёк", on_delete=models.CASCADE, related_name="entries",
    )
    amount = models.IntegerField("сумма")  # плюс — начисление, минус — трата или возврат
    reason = models.CharField("причина", max_length=30, choices=Reason.choices)
    # За что именно заплатили: «material:317», «likes:r42». Ключ, а не подпись — по нему
    # rewards.py и понимает, что уже оплачено (подробности — в его docstring).
    key = models.CharField("за что", max_length=64, blank=True)
    note = models.CharField("примечание", max_length=200, blank=True)
    # Баланс на момент операции: иначе каждую строку истории пришлось бы досчитывать
    # суммой всей предыдущей ленты.
    balance_after = models.IntegerField("баланс после")
    created = models.DateTimeField("когда", default=timezone.now)

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
