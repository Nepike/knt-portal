from django.contrib import admin

from .models import Comment, Material


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "year", "status", "uploader")
    list_filter = ("status", "subject")
    search_fields = ("title", "synopsis")
    autocomplete_fields = ("uploader",)
    filter_horizontal = ("teachers", "terms")


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("material", "author", "created")
    search_fields = ("material__title", "author__email")
    autocomplete_fields = ("material", "author")
