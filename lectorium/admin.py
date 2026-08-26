from django.contrib import admin

from .models import Lecture, Playlist


class LectureInline(admin.TabularInline):
    """Лекции правятся внутри плейлиста: сами по себе они не существуют.

    Обычным путём запись заводится сдачей файла на странице курса, и папку набора
    проставляет пекарня. Руками сюда ходят за другим: привязать набор, испечённый
    отдельно, — вписать его префикс. Что набор играет, покажет /lectures/check/.
    """

    model = Lecture
    extra = 0
    fields = ("order", "title", "prefix", "duration")
    ordering = ("order", "id")


@admin.register(Playlist)
class PlaylistAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "year", "status", "lectures_count", "uploader")
    list_filter = ("status", "subject")
    search_fields = ("title", "synopsis")
    autocomplete_fields = ("uploader",)
    filter_horizontal = ("teachers", "terms")
    inlines = [LectureInline]

    @admin.display(description="лекций")
    def lectures_count(self, playlist):
        return playlist.lectures.count()
