from django.contrib import admin

from .models import Material


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "year", "status", "uploader")
    list_filter = ("status", "subject")
    search_fields = ("title", "synopsis")
    autocomplete_fields = ("uploader",)
    filter_horizontal = ("teachers", "terms")
