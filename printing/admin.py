from datetime import timedelta

from django.contrib import admin, messages
from django.utils import timezone

from . import services
from .models import Printer, PrintAccount, PrinterReading, PrintJob
from .pricing import rubles


@admin.register(Printer)
class PrinterAdmin(admin.ModelAdmin):
    list_display = ("name", "place", "queue", "address", "active", "state", "toner_percent", "seen_at")
    fieldsets = (
        (None, {"fields": ("name", "place", "queue", "address", "active", "duplex")}),
        ("Себестоимость", {"fields": ("paper_sheet", "toner_price", "toner_yield", "drum_price", "drum_yield", "toner_factor")}),
        ("Что прислал агент", {"fields": ("state", "state_text", "toner_percent", "page_counter", "seen_at", "alert")}),
    )
    readonly_fields = ("state", "state_text", "toner_percent", "page_counter", "seen_at", "alert")


class WeekFilter(admin.SimpleListFilter):
    title = "неделя"
    parameter_name = "week"

    def lookups(self, request, model_admin):
        return (("0", "эта"), ("1", "прошлая"), ("4", "последние четыре"))

    def queryset(self, request, queryset):
        if self.value() not in {"0", "1", "4"}:
            return queryset
        start = services.week_start()
        if self.value() == "1":
            return queryset.filter(submitted_at__gte=start - timedelta(weeks=1), submitted_at__lt=start)
        return queryset.filter(submitted_at__gte=start - timedelta(weeks=3 if self.value() == "4" else 0))


@admin.register(PrintJob)
class PrintJobAdmin(admin.ModelAdmin):
    list_display = ("created", "who", "name", "status", "pages", "sides", "price", "printer", "outside_m4")
    list_filter = ("status", WeekFilter, "printer", "outside_m4")
    # Человек — поиском, а не фильтром: список фильтра вывел бы сбоку все триста фамилий.
    search_fields = ("user__email", "user__surname", "user__name", "name")
    list_select_related = ("user", "printer")
    actions = ("mark_failed",)
    # Всё пишут сайт и агент; руками — только действие ниже.
    readonly_fields = [field.name for field in PrintJob._meta.fields]

    def has_add_permission(self, request):
        return False

    @admin.display(description="кто", ordering="user__surname")
    def who(self, job):
        return job.user.full_name

    @admin.display(description="себестоимость", ordering="cost")
    def price(self, job):
        return rubles(job.cost) if job.sides else "—"

    @admin.action(description="Пометить «не вышло» (страницы вернутся в лимит)")
    def mark_failed(self, request, queryset):
        # Для `unknown`, когда у лотка проверили, что листов нет, и для зависшего в очереди.
        # Напечатанное и закрытое не трогаем: «не вышло» про done было бы неправдой.
        stuck = queryset.filter(status__in=(*PrintJob.ACTIVE, PrintJob.Status.UNKNOWN))
        done = stuck.update(
            status=PrintJob.Status.FAILED, reason="отмечено вручную в админке", finished_at=timezone.now(),
        )
        self.message_user(request, f"Помечено «не вышло»: {done}", messages.SUCCESS if done else messages.WARNING)


@admin.register(PrintAccount)
class PrintAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "weekly_limit", "this_week")
    search_fields = ("user__email", "user__surname", "user__name")
    autocomplete_fields = ("user",)

    @admin.display(description="на этой неделе")
    def this_week(self, account):
        return services.used(account.user)


@admin.register(PrinterReading)
class PrinterReadingAdmin(admin.ModelAdmin):
    list_display = ("at", "printer", "page_counter", "toner_percent", "state")
    list_filter = ("printer",)
    readonly_fields = ("printer", "at", "page_counter", "toner_percent", "state")

    def has_add_permission(self, request):
        return False
