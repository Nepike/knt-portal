from django.contrib import admin

from .models import Bakery, MediaJob


@admin.register(MediaJob)
class MediaJobAdmin(admin.ModelAdmin):
    list_display = ("__str__", "lecture", "claimed_by", "attempts", "tries", "created", "note")
    list_filter = ("status", "recipe")
    search_fields = ("source", "prefix", "note")
    # Всё, кроме состояния, пишут ручки приёмки. Руками — только вернуть задание в очередь («ждёт»).
    readonly_fields = ("recipe", "source", "lecture", "prefix", "manifest",
                       "claimed_by", "claimed_at", "attempts", "tries", "created", "updated")

    def save_model(self, request, obj, form, change):
        # Возврат в очередь — новый заход: сбрасываем `tries` (иначе MAX_TRIES закроет задание
        # на первом же `claim`), а не `attempts` — тот живёт в токене.
        if "status" in form.changed_data and obj.status == MediaJob.Status.WAITING:
            obj.tries = 0
        super().save_model(request, obj, form, change)


@admin.register(Bakery)
class BakeryAdmin(admin.ModelAdmin):
    """Только смотреть: строки пишут запросы пекарни (intake.health). Удалить можно — машину,
    которой больше нет, чтобы её неполадка не висела в Пульсе."""

    list_display = ("name", "seen_at", "problem")
    readonly_fields = ("name", "seen_at", "problem", "alert")

    def has_add_permission(self, request):
        return False
