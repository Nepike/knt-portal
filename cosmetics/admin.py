from django.contrib import admin
from django.utils.html import format_html

from attachments.media import media_url

from .forms import CosmeticItemForm
from .models import CosmeticItem, UserItem


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
        """Своя цена или та, что вышла из редкости, — чтобы не считать в уме."""
        return obj.cost if obj.price is not None else f"{obj.cost} (по редкости)"

    @admin.display(description="видео", boolean=True, ordering="video")
    def live(self, obj):
        """В списке обложки стоячие, и без этой отметки не видно, какие вещи анимированы."""
        return bool(obj.video)

    @admin.display(description="у скольких", ordering="_owners")
    def owned(self, obj):
        return obj._owners


@admin.register(UserItem)
class UserItemAdmin(admin.ModelAdmin):
    list_display = ("user", "item", "equipped", "acquired")
    list_filter = ("equipped", "item__rarity")
    search_fields = ("user__email", "user__surname", "item__name")
    autocomplete_fields = ("user", "item")
    date_hierarchy = "acquired"
