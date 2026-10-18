from functools import partial

from django.contrib import admin
from django.db import transaction
from django.utils.html import format_html

from attachments.media import media_url

from .cases import odds, percent
from .forms import CaseForm, CosmeticItemForm
from .models import Case, CaseOpening, CosmeticItem, UserItem


@admin.register(CosmeticItem)
class CosmeticItemAdmin(admin.ModelAdmin):
    form = CosmeticItemForm
    list_display = ("preview", "name", "kind", "rarity", "worth", "live", "sold", "owned", "created")
    list_display_links = ("preview", "name")
    list_filter = ("kind", "rarity", "sold")
    list_editable = ("sold",)
    search_fields = ("name",)
    readonly_fields = ("preview_big", "source", "created")
    fields = (
        "preview_big", "name", "kind", "rarity", "note",
        "sold", "price", "image", "video", "source", "created",
    )

    def get_queryset(self, request):
        from django.db.models import Count

        return super().get_queryset(request).annotate(_owners=Count("owners"))

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Прежние файлы снимаем после коммита: страница идёт одной транзакцией, и при
        # откате запись останется со старым файлом. У формы списка поля stale нет.
        for storage, name in getattr(form, "stale", ()):
            transaction.on_commit(partial(storage.delete, name))

    @admin.display(description="")
    def preview(self, obj):
        """Обложка. В списке всегда картинка, даже у видео: страниц по 100 вещей,
        и сотня автоплеев положила бы браузер. lazy тоже обязателен — вместе они
        под 35 МБ, и без него список тянул бы весь каталог до первой отрисовки."""
        return format_html(
            '<img src="{}" loading="lazy" style="width:56px;height:56px;background:#334">',
            media_url(obj.image),
        )

    @admin.display(description="как выглядит")
    def preview_big(self, obj):
        """На странице вещи — то же, что увидит студент: видео играет, картинка стоит.
        Вещь тут одна, автоплей ничего не перегрузит."""
        if not obj.pk:
            return "—"
        box = "width:280px;background:#334;border-radius:12px"
        if obj.video:
            return format_html(
                '<video src="{}" poster="{}" autoplay muted loop playsinline style="{}"></video>',
                media_url(obj.video), media_url(obj.image), box,
            )
        return format_html('<img src="{}" style="{}">', media_url(obj.image), box)

    @admin.display(description="цена")
    def worth(self, obj):
        """Своя цена или та, что вышла из вида и редкости, — чтобы не считать в уме."""
        return obj.cost if obj.price is not None else f"{obj.cost} (по таблице)"

    @admin.display(description="видео", boolean=True, ordering="video")
    def live(self, obj):
        """В списке обложки стоячие, и без этой отметки не видно, какие вещи анимированы."""
        return bool(obj.video)

    @admin.display(description="у скольких", ordering="_owners")
    def owned(self, obj):
        return obj._owners


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    form = CaseForm
    list_display = ("name", "price", "active", "size", "opened", "order")
    list_display_links = ("name",)
    filter_horizontal = ("items",)
    readonly_fields = ("odds_and_worth", "created")
    fields = ("name", "price", "active", "order", "cover", "items", "odds_and_worth", "created")

    def get_queryset(self, request):
        from django.db.models import Count

        return super().get_queryset(request).annotate(
            _size=Count("items", distinct=True), _opened=Count("openings", distinct=True),
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        for storage, name in getattr(form, "stale", ()):
            transaction.on_commit(partial(storage.delete, name))

    @admin.display(description="вещей", ordering="_size")
    def size(self, obj):
        return obj._size

    @admin.display(description="открыли", ordering="_opened")
    def opened(self, obj):
        return obj._opened

    @admin.display(description="шансы и средняя цена выпавшего")
    def odds_and_worth(self, obj):
        """Чтобы цену кейса ставить не вслепую: средняя цена выпавшей вещи по нынешним
        ценам — для того, у кого из набора ещё ничего нет. Считается по сохранённому набору."""
        items = list(obj.items.all()) if obj.pk else []
        if not items:
            return "набор пуст"
        chances = odds(items)
        worth = 0
        for rarity, share in chances:
            costs = [item.cost for item in items if item.rarity == rarity]
            worth += share * sum(costs) / len(costs)
        shares = " · ".join(f"{CosmeticItem.Rarity(rarity).label} {percent(share)} %" for rarity, share in chances)
        return f"{shares}. Средняя цена выпавшего ≈ {round(worth)}, цена кейса {obj.price}"


@admin.register(CaseOpening)
class CaseOpeningAdmin(admin.ModelAdmin):
    """Журнал открытий только на просмотр: по нему шансы сверяются с фактом."""

    list_display = ("created", "user", "case", "rarity", "item", "paid", "refund")
    list_filter = ("case", "rarity")
    search_fields = ("user__email", "user__surname", "item__name")
    date_hierarchy = "created"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(UserItem)
class UserItemAdmin(admin.ModelAdmin):
    list_display = ("user", "item", "equipped", "acquired")
    list_filter = ("equipped", "item__rarity")
    search_fields = ("user__email", "user__surname", "item__name")
    autocomplete_fields = ("user", "item")
    date_hierarchy = "acquired"
