from django.contrib import admin

from .models import Chat, Membership, Message, Reaction


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    autocomplete_fields = ("user",)
    raw_id_fields = ("last_read",)


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = ("__str__", "kind", "created")
    list_filter = ("kind",)
    search_fields = ("title",)
    raw_id_fields = ("last_message",)
    inlines = [MembershipInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("__str__", "created", "edited", "deleted")
    list_filter = ("deleted",)
    search_fields = ("text",)
    autocomplete_fields = ("author",)
    raw_id_fields = ("chat", "reply_to")


@admin.register(Reaction)
class ReactionAdmin(admin.ModelAdmin):
    list_display = ("emoji", "user", "message", "created")
    raw_id_fields = ("message",)
    autocomplete_fields = ("user",)
