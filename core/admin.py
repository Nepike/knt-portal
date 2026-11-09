import csv

from django import forms
from django.contrib import admin
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import donors, inquiries
from .lockdown import GROUP, PASS
from .models import Failure, Funding, Inquiry, Lockdown, Subject, Supporter, Team, Term

admin.site.register(Subject)
admin.site.register(Term)
admin.site.register(Team)


class FundingForm(forms.ModelForm):
    export = forms.FileField(
        label="выгрузка Boosty", required=False,
        help_text="CSV с донатами (stat-donations.csv). Заменяет имена этого сбора: каждый человек один раз, "
                  "«Имя Ф.», без тех, кто в «не показывать». Почты и суммы не сохраняются.",
    )

    class Meta:
        model = Funding
        fields = ("since", "until", "skip")

    def clean_export(self):
        """Имена из файла — или None, если файла не было."""
        export = self.cleaned_data["export"]
        if not export:
            return None
        try:
            return donors.read(export.read())
        except UnicodeDecodeError:
            raise ValidationError("Файл не в UTF-8 — нужна выгрузка Boosty как есть, без пересохранения.")
        except (csv.Error, ValueError) as error:
            raise ValidationError(f"Не получилось прочитать выгрузку: {error}.")


class SupporterInline(admin.TabularInline):
    model = Supporter
    extra = 0


@admin.register(Funding)
class FundingAdmin(admin.ModelAdmin):
    form = FundingForm
    inlines = [SupporterInline]
    fields = ("since", "until", "skip", "export", "loaded")
    readonly_fields = ("loaded",)
    list_display = ("__str__", "people", "loaded")

    @admin.display(description="имён")
    def people(self, funding):
        return funding.supporters.count()

    def save_related(self, request, form, formsets, change):
        """Выгрузка заменяет имена целиком — после строк, поправленных руками в той же форме."""
        super().save_related(request, form, formsets, change)
        names = form.cleaned_data.get("export")
        if names is None:
            return
        funding = form.instance
        funding.supporters.all().delete()
        signed = donors.signatures(names, funding.skip.splitlines())
        Supporter.objects.bulk_create(Supporter(funding=funding, name=name[:60]) for name in signed)
        funding.loaded = timezone.localdate()
        funding.save(update_fields=["loaded"])
        self.message_user(request, f"Имён из выгрузки: {len(signed)}.")


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


@admin.register(Failure)
class FailureAdmin(admin.ModelAdmin):
    """Журнал сбоев только на просмотр: пишут его сами места сбоев (core/failures.py)."""

    list_display = ("last_at", "source", "text", "times", "created")
    list_filter = ("source",)
    search_fields = ("text",)
    date_hierarchy = "last_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    """Обращения из поддержки и контактов. Пишет их сама форма (core/inquiries.py); отсюда —
    только смотреть, удалять и вернуть в чат живого человека, которого отсеял фильтр."""

    list_display = ("created", "source", "verdict", "score", "who", "short")
    list_filter = ("verdict", "source")
    search_fields = ("text", "name", "contact")
    date_hierarchy = "created"
    fields = ("created", "source", "verdict", "author", "name", "contact", "topic", "text", "had_image",
              "score", "reasons_text", "seconds", "ip", "agent")
    readonly_fields = fields
    actions = ["release"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="кто")
    def who(self, inquiry):
        return inquiry.author.full_name if inquiry.author else f"{inquiry.name} · {inquiry.contact}".strip(" ·")

    @admin.display(description="текст")
    def short(self, inquiry):
        return inquiry.text[:80]

    @admin.display(description="за что")
    def reasons_text(self, inquiry):
        return inquiry.reasons_text()

    @admin.action(description="Не спам — переслать в чат", permissions=["view"])
    def release(self, request, queryset):
        caught = list(queryset.filter(verdict=Inquiry.Verdict.SPAM))
        for inquiry in caught:
            inquiries.release(request, inquiry)
            self.log_change(request, inquiry, "Не спам — переслано в чат")
        skipped = queryset.count() - len(caught)
        self.message_user(request, f"Переслано в чат: {len(caught)}." + (f" Не отсеянных среди выбранных: {skipped}, их не трогали." if skipped else ""))
