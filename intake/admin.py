from django.contrib import admin

from .models import MediaJob


@admin.register(MediaJob)
class MediaJobAdmin(admin.ModelAdmin):
    list_display = ("__str__", "lecture", "claimed_by", "attempts", "created", "note")
    list_filter = ("status", "recipe")
    search_fields = ("source", "prefix", "note")
    # Всё, кроме состояния, пишут ручки приёмки. Руками тут только возвращают
    # задание в очередь: поставил «ждёт» — и следующая пекарня возьмёт его снова.
    readonly_fields = ("recipe", "source", "lecture", "prefix", "manifest",
                       "claimed_by", "claimed_at", "attempts", "created", "updated")
