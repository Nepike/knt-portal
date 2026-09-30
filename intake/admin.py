from django.contrib import admin

from .models import MediaJob


@admin.register(MediaJob)
class MediaJobAdmin(admin.ModelAdmin):
    list_display = ("__str__", "lecture", "claimed_by", "attempts", "tries", "created", "note")
    list_filter = ("status", "recipe")
    search_fields = ("source", "prefix", "note")
    # Всё, кроме состояния, пишут ручки приёмки. Руками тут только возвращают
    # задание в очередь: поставил «ждёт» — и следующая пекарня возьмёт его снова.
    readonly_fields = ("recipe", "source", "lecture", "prefix", "manifest",
                       "claimed_by", "claimed_at", "attempts", "tries", "created", "updated")

    def save_model(self, request, obj, form, change):
        # Вернувший задание в очередь человек даёт ему новый заход, а не одну последнюю
        # выдачу: иначе закрытое по пределу (MAX_TRIES) задание закрылось бы снова на
        # первом же `claim`. Сбрасываем `tries`, а не `attempts` — тот живёт в токене.
        if "status" in form.changed_data and obj.status == MediaJob.Status.WAITING:
            obj.tries = 0
        super().save_model(request, obj, form, change)
