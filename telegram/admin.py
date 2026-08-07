from django.contrib import admin

from .models import TelegramChat


@admin.register(TelegramChat)
class TelegramChatAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "chat_id", "topic_id")
    search_fields = ("name", "description")
