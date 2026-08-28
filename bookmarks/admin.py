from django.contrib import admin

from .models import Bookmark


@admin.register(Bookmark)
class BookmarkAdmin(admin.ModelAdmin):
    list_display = ("user", "__str__", "kind", "created")
    list_filter = ("created",)
    autocomplete_fields = ("user",)
    readonly_fields = ("created",)
