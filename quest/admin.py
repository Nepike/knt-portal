from django.contrib import admin
from django.db.models import Count

from .models import Broadcast, Clue, Find, Fragment, Tally, Unlock


@admin.register(Clue)
class ClueAdmin(admin.ModelAdmin):
    list_display = ("place", "command", "text", "laugh", "active")
    list_filter = ("place", "active")
    list_editable = ("active",)
    search_fields = ("command", "text")


@admin.register(Fragment)
class FragmentAdmin(admin.ModelAdmin):
    list_display = ("number", "value", "note", "finders")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(found_by=Count("finds"))

    @admin.display(description="сдали", ordering="found_by")
    def finders(self, fragment):
        return fragment.found_by


@admin.register(Broadcast)
class BroadcastAdmin(admin.ModelAdmin):
    """Одна запись на сайт: какая лекция — передача и какая рамка — за ключ."""

    list_display = ("__str__", "lecture", "frame")

    def has_add_permission(self, request):
        return not Broadcast.objects.exists()


@admin.register(Unlock)
class UnlockAdmin(admin.ModelAdmin):
    """Кто ввёл ключ — только смотреть: пишет консоль (quest.keys.unlock), она же платит."""

    list_display = ("user", "at")
    search_fields = ("user__email", "user__surname")
    list_select_related = ("user",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Find)
class FindAdmin(admin.ModelAdmin):
    """Кто что сдал — только смотреть: находки пишет консоль (quest.keys.found)."""

    list_display = ("user", "fragment", "at")
    list_filter = ("fragment",)
    search_fields = ("user__email", "user__surname")
    list_select_related = ("user", "fragment")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Tally)
class TallyAdmin(admin.ModelAdmin):
    """Счётчики для праздничной страницы — только смотреть: считает консоль (quest.views.ask)."""

    list_display = ("name", "count")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
