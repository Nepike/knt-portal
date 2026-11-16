"""Косметика: предметы, редкости, слоты, инвентарь. Валюта и журнал — в economy."""

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
        # TODO (M5): значок под именем. Новому виду нужны плитка (.slot-* в input.css) и строка
        # в cosmetics/specs.py.

    class Rarity(models.TextChoices):
        """Ступени со старого сайта."""

        COMMON = "common", "обычная"
        RARE = "rare", "редкая"
        EPIC = "epic", "эпическая"
        LEGENDARY = "legendary", "легендарная"
        MYTHICAL = "mythical", "мифическая"

    # Ступени по возрастанию: в базе это строка, алфавит поставил бы legendary между epic и mythical.
    RARITY_ORDER = (Rarity.COMMON, Rarity.RARE, Rarity.EPIC, Rarity.LEGENDARY, Rarity.MYTHICAL)

    # Цена по виду и ступени. Рамка — мера, фон дороже (вся страница, почти всегда видео),
    # шапка дешевле. Новый вид — новая строка, иначе вещь стоит как самая дорогая (cost).
    PRICES = {
        Kind.PROFILE_HEADER: {
            Rarity.COMMON: 50, Rarity.RARE: 150, Rarity.EPIC: 350, Rarity.LEGENDARY: 900, Rarity.MYTHICAL: 2500,
        },
        Kind.AVATAR_FRAME: {
            Rarity.COMMON: 100, Rarity.RARE: 250, Rarity.EPIC: 600, Rarity.LEGENDARY: 1500, Rarity.MYTHICAL: 4000,
        },
        Kind.PROFILE_BACKGROUND: {
            Rarity.COMMON: 150, Rarity.RARE: 400, Rarity.EPIC: 900, Rarity.LEGENDARY: 2200, Rarity.MYTHICAL: 6000,
        },
    }

    name = models.CharField("название", max_length=100, unique=True)
    kind = models.CharField("вид", max_length=20, choices=Kind.choices, default=Kind.AVATAR_FRAME, db_index=True)
    rarity = models.CharField("редкость", max_length=20, choices=Rarity.choices, default=Rarity.COMMON, db_index=True)
    note = models.CharField("описание", max_length=200, blank=True)

    # `image` — вещь картинкой: у рамки сама APNG-анимация, у шапки и фона — постер, КАДР этого
    # же видео (команда — docs/media-pipeline.md). `video` необязателен: есть — рисуем <video>.
    # Видео только у непрозрачных вещей: прозрачного, работающего везде, нет — рамки остаются APNG.
    image = models.ImageField(
        "картинка", upload_to=frame_upload_to, storage=media_storage,
        help_text="у анимированной вещи — первый кадр её видео",
    )
    video = models.FileField(
        "видео", upload_to=frame_upload_to, storage=media_storage, blank=True,
        help_text="mp4 по спеке, испечённый снаружи — сайт не конвертирует",
    )

    # Откуда вещь: «legacy:14», «file:9f3c….png», «generated:Ночь» — по нему повторный импорт
    # узнаёт перенесённое (имя половине рамок придумываем сами).
    source = models.CharField("источник", max_length=120, blank=True)
    created = models.DateTimeField("добавлена", default=timezone.now)

    sold = models.BooleanField("продаётся", default=True)
    price = models.PositiveIntegerField(
        "своя цена", null=True, blank=True, help_text="пусто — по виду и редкости",
    )

    class Meta:
        verbose_name = "предмет"
        verbose_name_plural = "предметы"
        ordering = ["kind", "rarity", "name"]
        constraints = [
            # Пустой источник (добавлено руками) — сколько угодно.
            models.UniqueConstraint(
                fields=["source"], condition=~models.Q(source=""), name="one_item_per_source",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_rarity_display()})"

    @property
    def cost(self):
        """Сколько стоит. Вид или ступень не из таблицы — самая высокая цена: ошибиться в «дорого» не страшно."""
        if self.price is not None:
            return self.price
        try:
            return self.PRICES[self.kind][self.rarity]
        except KeyError:
            return max(price for row in self.PRICES.values() for price in row.values())

    def slot_title(self):
        return slot_heading(self.kind)

    def save(self, *args, **kwargs):
        # Новой вещи владельцев нет: после super().save() отличить создание по pk уже нельзя.
        fresh = self._state.adding
        super().save(*args, **kwargs)
        # Вид продублирован в UserItem: правка вида едет следом и снимает вещь — в новом слоте
        # может быть надето своё.
        if not fresh:
            self.owners.exclude(kind=self.kind).update(kind=self.kind, equipped=False)


SLOT_TITLES = {
    CosmeticItem.Kind.AVATAR_FRAME: "Рамки аватара",
    CosmeticItem.Kind.PROFILE_HEADER: "Шапки профиля",
    CosmeticItem.Kind.PROFILE_BACKGROUND: "Фоны профиля",
}


def slot_heading(kind):
    """Подпись блока в инвентаре и витрине — во множественном числе, в отличие от get_kind_display()."""
    return SLOT_TITLES.get(kind, "Прочее")


class UserItem(models.Model):
    """Что у человека есть и что из этого надето."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="владелец", on_delete=models.CASCADE, related_name="items",
    )
    item = models.ForeignKey(CosmeticItem, verbose_name="предмет", on_delete=models.CASCADE, related_name="owners")
    # Копия item.kind: путь по внешнему ключу в UniqueConstraint не пускают. Ставится в save()
    # здесь и в CosmeticItem.save.
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


class Case(models.Model):
    """Кейс: цена и набор вещей. Шансы — общие, по ступеням (cosmetics/cases.py). В набор идут
    и вещи вне продажи. Набор за магазином не следит: новую рамку в кейс добавляют руками."""

    name = models.CharField("название", max_length=100, unique=True)
    price = models.PositiveIntegerField("цена")
    items = models.ManyToManyField(CosmeticItem, verbose_name="вещи", related_name="cases")
    cover = models.ImageField(
        "обложка", upload_to=frame_upload_to, storage=media_storage, blank=True,
        help_text="пусто — веер из трёх самых редких вещей набора",
    )
    active = models.BooleanField("открывается", default=False, help_text="пустой кейс включить нельзя")
    order = models.PositiveSmallIntegerField("порядок", default=0, help_text="меньше — раньше на витрине")
    created = models.DateTimeField("добавлен", default=timezone.now)

    class Meta:
        verbose_name = "кейс"
        verbose_name_plural = "кейсы"
        ordering = ["order", "price", "pk"]

    def __str__(self):
        return self.name


class CaseOpening(models.Model):
    """Журнал открытий: по нему сверяют шансы и видят первый, подарочный кейс. Кейс с
    открытиями не удаляют — выключают."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.CASCADE, related_name="case_openings",
    )
    case = models.ForeignKey(Case, verbose_name="кейс", on_delete=models.PROTECT, related_name="openings")
    # Пусто — ступень была собрана: вещи нет, вернули половину.
    item = models.ForeignKey(
        CosmeticItem, verbose_name="выпало", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    rarity = models.CharField("ступень", max_length=20, choices=CosmeticItem.Rarity.choices)
    paid = models.PositiveIntegerField("цена", help_text="0 — первый, подарочный")
    refund = models.PositiveIntegerField("вернули", default=0)
    created = models.DateTimeField("когда", default=timezone.now)

    class Meta:
        verbose_name = "открытие кейса"
        verbose_name_plural = "открытия кейсов"
        ordering = ["-id"]

    def __str__(self):
        return f"{self.user}: {self.case}"
