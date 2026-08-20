from django.contrib import admin
from django.utils.html import format_html

from attachments.media import media_url

from .models import CosmeticItem, UserItem


@admin.register(CosmeticItem)
class CosmeticItemAdmin(admin.ModelAdmin):
    list_display = ("preview", "name", "kind", "rarity", "owned", "created")
    list_display_links = ("preview", "name")
    list_filter = ("kind", "rarity")
    search_fields = ("name",)
    readonly_fields = ("preview_big", "source", "created")
    fields = ("preview_big", "name", "kind", "rarity", "note", "image", "still", "source", "created")

    def get_queryset(self, request):
        from django.db.models import Count

        return super().get_queryset(request).annotate(_owners=Count("owners"))

    @admin.display(description="")
    def preview(self, obj):
        """Обложка, а не анимация: в списке предметов десятки, и сорок анимаций разом
        превратили бы страницу админки в слайд-шоу."""
        return format_html('<img src="{}" style="width:56px;height:56px;background:#334">', media_url(obj.still))

    @admin.display(description="как выглядит")
    def preview_big(self, obj):
        if not obj.pk:
            return "—"
        return format_html(
            '<img src="{}" style="width:160px;height:160px;background:#334;border-radius:12px">', media_url(obj.image),
        )

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
