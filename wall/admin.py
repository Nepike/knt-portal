from django.contrib import admin

from . import palette
from .models import Board, Placement, ProtectedArea, WallProfile


@admin.register(Board)
class BoardAdmin(admin.ModelAdmin):
    list_display = ("title", "width", "height", "is_active", "created", "closed")


@admin.register(WallProfile)
class WallProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "charges", "painted", "banned_until")
    search_fields = ("user__email", "user__surname")
    # Заряды и счётчик — результат игры, правятся только сервисом. Бан здесь руками:
    # на самой Стене он вешается на автора клетки, а снять его иногда надо, не разыскивая
    # по доске, чей пиксель.
    readonly_fields = ("user", "charges", "charged_at", "painted")
    fields = ("user", "charges", "charged_at", "painted", "banned_until")


@admin.register(ProtectedArea)
class ProtectedAreaAdmin(admin.ModelAdmin):
    list_display = ("board", "x1", "y1", "x2", "y2", "note", "by", "created")
    list_filter = ("board",)


@admin.register(Placement)
class PlacementAdmin(admin.ModelAdmin):
    list_display = ("created", "board", "x", "y", "color_name", "user")
    list_filter = ("board",)
    search_fields = ("user__email", "user__surname")
    date_hierarchy = "created"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return obj is None  # журнал не правится

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="цвет")
    def color_name(self, obj):
        return palette.get(obj.color).name
