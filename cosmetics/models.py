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
        PROFILE_BACKGROUND = "profile_background", "фон профиля"
        # TODO (M5): значок под именем. Новому виду нужны своя плитка (.slot-* в input.css)
        # и своя строка в cosmetics/specs.py — иначе загрузят что попало.

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

    # Цена по ступени. Проставлять её каждой из полусотни вещей руками незачем — редкость
    # ровно про это и есть. Цифры от начислений: медиана заработанного 510, а 127 человек
    # из 324 получат только стартовые 500, и им должно хватать на первую обычную вещь.
    RARITY_PRICE = {
        Rarity.COMMON: 250,
        Rarity.RARE: 600,
        Rarity.EPIC: 1200,
        Rarity.LEGENDARY: 2500,
        Rarity.MYTHICAL: 5000,
    }

    name = models.CharField("название", max_length=100, unique=True)
    kind = models.CharField("вид", max_length=20, choices=Kind.choices, default=Kind.AVATAR_FRAME, db_index=True)
    rarity = models.CharField("редкость", max_length=20, choices=Rarity.choices, default=Rarity.COMMON, db_index=True)
    note = models.CharField("описание", max_length=200, blank=True)

    # `image` — «как вещь выглядит картинкой»: у рамки это сама APNG-анимация, у шапки
    # и фона — постер. `video` необязателен и старше: есть он — рисуем <video>, нет — <img>.
    #
    # Видео только у непрозрачных вещей. Прозрачного, работающего везде, не существует:
    # VP9 умеет альфу, но её не декодирует Safari; HEVC с альфой умеет только Safari.
    # Ради картинки 224×224 это не окупается, поэтому рамки остаются APNG.
    image = models.ImageField("картинка", upload_to=frame_upload_to, storage=media_storage)
    video = models.FileField("видео", upload_to=frame_upload_to, storage=media_storage, blank=True)

    # Откуда вещь взялась: «legacy:14», «file:9f3c….png», «generated:Ночь». По нему
    # повторный импорт узнаёт уже перенесённое. По имени узнавать нельзя: половине рамок
    # имя придумываем мы сами — на втором заходе придумали бы другое и разложили дважды.
    source = models.CharField("источник", max_length=120, blank=True)
    created = models.DateTimeField("добавлена", default=timezone.now)

    sold = models.BooleanField("продаётся", default=True)
    price = models.PositiveIntegerField(
        "своя цена", null=True, blank=True, help_text="пусто — по редкости",
    )

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

    @property
    def cost(self):
        """Сколько стоит. Неизвестная ступень — самая дорогая: ошибиться в сторону
        «дорого» не страшно, в сторону «даром» — раздать полбакета."""
        if self.price is not None:
            return self.price
        return self.RARITY_PRICE.get(self.rarity, self.RARITY_PRICE[self.Rarity.MYTHICAL])

    def slot_title(self):
        return slot_heading(self.kind)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Вид продублирован в UserItem, и правка вида в админке обязана поехать следом,
        # иначе вещь останется в чужом блоке инвентаря и займёт не тот слот. Снимаем
        # заодно: в новом слоте у человека уже может быть надето своё.
        if self.pk:
            self.owners.exclude(kind=self.kind).update(kind=self.kind, equipped=False)


SLOT_TITLES = {
    CosmeticItem.Kind.AVATAR_FRAME: "Рамки аватара",
    CosmeticItem.Kind.PROFILE_HEADER: "Шапки профиля",
    CosmeticItem.Kind.PROFILE_BACKGROUND: "Фоны профиля",
}


def slot_heading(kind):
    """Подпись блока в инвентаре и в витрине. Не get_kind_display(): там название вещи
    в единственном числе («рамка аватара»), а над блоком нужна множественная."""
    return SLOT_TITLES.get(kind, "Прочее")


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
        return slot_heading(self.kind)

    def save(self, *args, **kwargs):
        self.kind = self.item.kind  # предмет уже загружен везде, откуда сюда приходят
        super().save(*args, **kwargs)
