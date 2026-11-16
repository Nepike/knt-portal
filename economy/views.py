"""Своя история кошелька — только своя и без номера человека в адресе: по журналу видно,
что он покупал.
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
