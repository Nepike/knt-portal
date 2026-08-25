"""Своя история кошелька.

Только своя, и без адреса с номером человека: по журналу видно и что человек покупал,
и насколько он активен, — а в профиле у остальных остаётся один баланс.
"""

from django.core.paginator import Paginator
from django.shortcuts import render

from .models import BalanceLog

PAGE_SIZE = 50  # операций в порции


def wallet(request):
    entries = BalanceLog.objects.filter(wallet__user=request.user)
    page = Paginator(entries, PAGE_SIZE).get_page(request.GET.get("page"))
    context = {"page": page, "entries": page.object_list}

    # Подгрузка следующей порции приезжает по htmx одним списком, без страницы вокруг.
    if request.headers.get("HX-Request") and request.GET.get("page"):
        return render(request, "economy/_entries.html", context)
    return render(request, "economy/wallet.html", context)
