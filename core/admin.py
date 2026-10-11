from django.contrib import admin
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.utils.html import format_html, format_html_join

from .lockdown import GROUP, PASS
from .models import Lockdown, Subject, Team, Term

admin.site.register(Subject)
admin.site.register(Term)
admin.site.register(Team)


@admin.register(Lockdown)
class LockdownAdmin(admin.ModelAdmin):
    """Запись одна: список сразу ведёт в неё, второй не завести и эту не удалить."""

    fields = ("active", "text", "due", "deadline", "contact", "let_in")
    readonly_fields = ("let_in",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        lock, _ = Lockdown.objects.get_or_create(pk=1)
        return redirect("admin:core_lockdown_change", lock.pk)

    @admin.display(description="кто проходит")
    def let_in(self, lock):
        people = ModelBackend().with_perm(PASS).order_by("surname", "name")
        names = format_html_join("", "{}<br>", ((user.full_name,) for user in people))
        return format_html("{}Добавить человека: «Пользователи» → его страница → группа «{}».", names, GROUP)
