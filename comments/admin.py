from django.contrib import admin

from .models import Comment


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("__str__", "author", "created")
    list_filter = ("created",)
    search_fields = ("text", "material__title", "lecture__title", "author__email")
    autocomplete_fields = ("author",)
    raw_id_fields = ("material", "lecture", "parent")
