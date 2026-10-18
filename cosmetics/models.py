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

    # Цена по виду и ступени. Проставлять её каждой из полусотни вещей руками незачем:
    # ступень ровно про это и есть, а вид решает, сколько места вещь занимает. Рамка — мера;
    # фон примерно в полтора раза дороже — кроет всю страницу профиля и почти всегда видео;
    # шапка дешевле — узкая полоса под именем.
    # Цифры — решение пользователя 18 окт 2026: со стартовых 500 должно хватать не на одну
    # вещь, а хотя бы на две редкие рамки. У половины людей на счету столько и есть.
    # Новый вид вещи — новая строка; без неё вещь стоит как самая дорогая (см. cost).
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

    # `image` — «как вещь выглядит картинкой»: у рамки это сама APNG-анимация, у шапки
    # и фона — постер. `video` необязателен и старше: есть он — рисуем <video>, нет — <img>.
    # Оба поля у одной вещи, а не два предмета: постер виден в те доли секунды, пока
    # видео не готово, и в витрине до подстановки источника. Поэтому постер обязан быть
    # КАДРОМ ЭТОГО ЖЕ видео — от чужой картинки виден скачок (проверить это нечем,
    # держится на команде из docs/media-pipeline.md).
    #
    # Видео только у непрозрачных вещей. Прозрачного, работающего везде, не существует:
    # VP9 умеет альфу, но её не декодирует Safari; HEVC с альфой умеет только Safari.
    # Ради картинки 224×224 это не окупается, поэтому рамки остаются APNG.
    image = models.ImageField(
        "картинка", upload_to=frame_upload_to, storage=media_storage,
        help_text="у анимированной вещи — первый кадр её видео",
    )
    video = models.FileField(
        "видео", upload_to=frame_upload_to, storage=media_storage, blank=True,
        help_text="mp4 по спеке, испечённый снаружи — сайт не конвертирует",
    )

    # Откуда вещь взялась: «legacy:14», «file:9f3c….png», «generated:Ночь». По нему
    # повторный импорт узнаёт уже перенесённое. По имени узнавать нельзя: половине рамок
    # имя придумываем мы сами — на втором заходе придумали бы другое и разложили дважды.
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
            # Пустой источник у добавленных руками — таких может быть сколько угодно.
            models.UniqueConstraint(
                fields=["source"], condition=~models.Q(source=""), name="one_item_per_source",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_rarity_display()})"

    @property
    def cost(self):
        """Сколько стоит. Вид или ступень не из таблицы — самая высокая цена в ней:
        ошибиться в сторону «дорого» не страшно, в сторону «даром» — раздать полбакета."""
        if self.price is not None:
            return self.price
        try:
            return self.PRICES[self.kind][self.rarity]
        except KeyError:
            return max(price for row in self.PRICES.values() for price in row.values())

    def slot_title(self):
        return slot_heading(self.kind)

    def save(self, *args, **kwargs):
        # Новой вещи владельцев ещё нет, и запрос ниже ей незачем: `self.pk` после
        # super().save() уже стоит всегда и отличить создание от правки не помог бы.
        fresh = self._state.adding
        super().save(*args, **kwargs)
        # Вид продублирован в UserItem, и правка вида в админке обязана поехать следом,
        # иначе вещь останется в чужом блоке инвентаря и займёт не тот слот. Снимаем
        # заодно: в новом слоте у человека уже может быть надето своё.
        if not fresh:
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


class Case(models.Model):
    """Кейс: коробка с ценой и набором вещей. Открыл — выпала одна вещь из набора.

    Шансы общие на все кейсы и живут в cosmetics/cases.py — по ступеням, а не по вещам:
    взвешивать руками каждую из полусотни вещей незачем. В набор идут любые вещи, и те,
    которых нет в продаже (sold выключен), — так у кейса бывают свои, эксклюзивные.
    Набор не следит за магазином сам: новую рамку в кейс «Рамки» добавляют руками.
    """

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
    """Журнал открытий: кто, какой кейс, что выпало, за сколько.

    По нему шансы сверяются с фактом, и по нему же видно, был ли у человека первый,
    подарочный кейс. Кейс с открытиями не удаляется — его выключают.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="кто", on_delete=models.CASCADE, related_name="case_openings",
    )
    case = models.ForeignKey(Case, verbose_name="кейс", on_delete=models.PROTECT, related_name="openings")
    # Пусто — выпала ступень, которая у человека уже собрана целиком: вещи нет, вернули половину.
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
