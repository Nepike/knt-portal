from django import forms
from django.contrib import admin

from .models import BalanceLog, Wallet
from .rewards import AUTOMATIC
from .services import credit, spend


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("user", "balance")
    search_fields = ("user__email", "user__surname")
    readonly_fields = ("user", "balance")  # баланс меняется операцией, а не правкой поля

    def has_add_permission(self, request):
        return False  # кошелёк заводится первой же операцией


class GrantForm(forms.ModelForm):
    """Проверяем достаточность здесь, чтобы модератор увидел обычную ошибку формы,
    а не исключение из сервиса на полпути к сохранению."""

    class Meta:
        model = BalanceLog
        fields = ("wallet", "amount", "reason", "note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Причины пересчёта руками не выдаются. Такая строка приходит без ключа, а
        # rewards читает её как «за это уже заплачено» — и человек молча недополучил бы
        # свою автоматическую награду.
        self.fields["reason"].choices = [
            pair for pair in BalanceLog.Reason.choices if pair[0] not in AUTOMATIC
        ]

    def clean(self):
        data = super().clean()
        wallet, amount = data.get("wallet"), data.get("amount")
        if wallet and amount is not None:
            if amount == 0:
                raise forms.ValidationError("нулевая операция ничего не меняет")
            if wallet.balance + amount < 0:
                raise forms.ValidationError(f"на балансе только {wallet.balance}")
        return data


@admin.register(BalanceLog)
class BalanceLogAdmin(admin.ModelAdmin):
    """Добавление строки здесь — и есть выдача валюты вручную.

    Записанное не правится и не удаляется: журнал на то и журнал, а кэш баланса
    считается по нему и от задним числом исправленной строки разъедется.
    """

    form = GrantForm
    list_display = ("created", "wallet", "amount", "reason", "note")
    list_filter = ("reason",)
    search_fields = ("wallet__user__email", "wallet__user__surname")
    autocomplete_fields = ("wallet",)
    date_hierarchy = "created"

    def has_change_permission(self, request, obj=None):
        return obj is None  # список открывается, отдельная запись — только на просмотр

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        # Пишет сервис, а не форма: только он трогает кэш баланса и держит блокировку.
        # Ключи возвращаем в obj, иначе админка не соберёт ссылку на созданную запись.
        move = credit if obj.amount > 0 else spend
        entry = move(obj.wallet.user, abs(obj.amount), obj.reason, obj.note)
        obj.pk, obj.balance_after, obj.created = entry.pk, entry.balance_after, entry.created
