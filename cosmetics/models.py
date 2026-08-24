"""Косметика: предметы, редкости, слоты, инвентарь.

Своё приложение, а не часть economy: там валюта и журнал операций, здесь вещи и то,
что человек носит. Пересекутся они позже, в магазине, — и пусть пересекутся явно.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from attachments.storage import media_storage, random_key


def frame_upload_to(instance, filename):
    return random_key("cosmetics", filename)


class CosmeticItem(models.Model):
    class Kind(models.TextChoices):
        AVATAR_FRAME = "avatar_frame", "рамка аватара"
        PROFILE_HEADER = "profile_header", "шапка профиля"
        # TODO (M5): значок под именем — новому виду нужна и своя плитка (.slot-* в input.css)

    class Rarity(models.TextChoices):
        """Ступени со старого сайта. Подписи там были матерные — смысл сохранён, слова нет."""

        COMMON = "common", "обычная"
        RARE = "rare", "редкая"
        EPIC = "epic", "эпическая"
        LEGENDARY = "legendary", "легендарная"
        MYTHICAL = "mythical", "мифическая"

    # Ступени по возрастанию. В базе редкость — строка, и сортировка по ней алфавитная:
    # «legendary» оказывался бы между «epic» и «mythical». Отсюда явный порядок.
    RARITY_ORDER = (Rarity.COMMON, Rarity.RARE, Rarity.EPIC, Rarity.LEGENDARY, Rarity.MYTHICAL)

    name = models.CharField("название", max_length=100, unique=True)
    kind = models.CharField("вид", max_length=20, choices=Kind.choices, default=Kind.AVATAR_FRAME, db_index=True)
    rarity = models.CharField("редкость", max_length=20, choices=Rarity.choices, default=Rarity.COMMON, db_index=True)
    note = models.CharField("описание", max_length=200, blank=True)

    # Анимация и один кадр из неё. Второй нужен спискам: в инвентаре предметов десятки,
    # и сорок анимированных картинок разом — это мегабайты и подтормаживающая страница.
    image = models.ImageField("анимация", upload_to=frame_upload_to, storage=media_storage)
    still = models.ImageField("кадр", upload_to=frame_upload_to, storage=media_storage)

    # Откуда вещь взялась: «legacy:14», «file:9f3c….png», «generated:Ночь». По нему
    # повторный импорт узнаёт уже перенесённое. По имени узнавать нельзя: половине рамок
    # имя придумываем мы сами — на втором заходе придумали бы другое и разложили дважды.
    source = models.CharField("источник", max_length=120, blank=True)
    created = models.DateTimeField("добавлена", default=timezone.now)

    class Meta:
        verbose_name = "предмет"
        verbose_name_plural = "предметы"
        ordering = ["kind", "rarity", "name"]
        constraints = [
            # Пустой источник у добавленных руками — таких может быть сколько угодно.
            models.UniqueConstraint(
                fields=["source"], condition=~models.Q(source=""), name="one_item_per_source",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_rarity_display()})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Вид продублирован в UserItem, и правка вида в админке обязана поехать следом,
        # иначе вещь останется в чужом блоке инвентаря и займёт не тот слот. Снимаем
        # заодно: в новом слоте у человека уже может быть надето своё.
        if self.pk:
            self.owners.exclude(kind=self.kind).update(kind=self.kind, equipped=False)


# Заголовок блока в инвентаре. Не get_kind_display: там название вещи в единственном
# числе («рамка аватара»), а над блоком нужна множественная подпись.
SLOT_TITLES = {
    CosmeticItem.Kind.AVATAR_FRAME: "Рамки аватара",
    CosmeticItem.Kind.PROFILE_HEADER: "Шапки профиля",
}


class UserItem(models.Model):
    """Что у человека есть и что из этого надето."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="владелец", on_delete=models.CASCADE, related_name="items",
    )
    item = models.ForeignKey(CosmeticItem, verbose_name="предмет", on_delete=models.CASCADE, related_name="owners")
    # Копия item.kind: путь по внешнему ключу в UniqueConstraint не пускают, а без
    # ограничения «одна вещь на слот» держалось бы только на честном слове кода.
    # Проставляется в save() и здесь, и на стороне предмета (CosmeticItem.save).
    kind = models.CharField("вид", max_length=20, choices=CosmeticItem.Kind.choices, editable=False)
    equipped = models.BooleanField("надето", default=False)
    acquired = models.DateTimeField("получено", default=timezone.now)

    class Meta:
        verbose_name = "вещь"
        verbose_name_plural = "инвентарь"
        ordering = ["-acquired"]
        constraints = [
            models.UniqueConstraint(fields=["user", "item"], name="one_copy_per_person"),
            models.UniqueConstraint(
                fields=["user", "kind"], condition=models.Q(equipped=True), name="one_equipped_per_slot",
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.item}"

    def slot_title(self):
        """Подпись блока, в который вещь попадёт в инвентаре."""
        return SLOT_TITLES.get(self.kind, self.get_kind_display())

    def save(self, *args, **kwargs):
        self.kind = self.item.kind  # предмет уже загружен везде, откуда сюда приходят
        super().save(*args, **kwargs)
