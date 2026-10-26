"""«Токены и магазин»: откуда токены берутся и куда уходят, кейсы, балансы, ручные операции."""

from django.db.models import Count, Q, Sum

from cosmetics.cases import ODDS
from cosmetics.models import Case, CaseOpening, CosmeticItem, UserItem
from economy.models import BalanceLog, Wallet
from economy.services import mismatched, recount

from .access import logged
from .people import Refused

R = BalanceLog.Reason
SPENDING = (R.PURCHASE, R.CASE)
# Меньше открытий — и расхождение с шансами ничего не значит: так и пишем.
SMALL_SAMPLE = 100


def overview(edge):
    reasons = dict(R.choices)
    wallets = Wallet.objects.aggregate(total=Sum("balance"), debt=Count("id", filter=Q(balance__lt=0)))
    period = BalanceLog.objects.filter(created__gte=edge).aggregate(
        plus=Sum("amount", filter=Q(amount__gt=0)), plus_n=Count("id", filter=Q(amount__gt=0)),
        minus=Sum("amount", filter=Q(amount__lt=0)), minus_n=Count("id", filter=Q(amount__lt=0)),
    )
    sources = list(
        BalanceLog.objects.filter(amount__gt=0).values_list("reason")
        .annotate(total=Sum("amount"), n=Count("id")).order_by("-total")
    )
    top = sources[0][1] if sources else 1
    earned = sum(total for _, total, _ in sources)
    sinks = dict(
        (reason, (-total, n)) for reason, total, n in
        BalanceLog.objects.filter(reason__in=SPENDING, amount__lt=0).values_list("reason")
        .annotate(total=Sum("amount"), n=Count("id")).order_by()
    )
    spent = sum(total for total, _ in sinks.values())
    kinds = dict(CosmeticItem.Kind.choices)
    return {
        "on_hands": wallets["total"] or 0,
        "in_debt": wallets["debt"],
        "period": {key: value or 0 for key, value in period.items()},
        "sources": [
            {"label": reasons.get(reason, reason), "total": total, "n": n, "share": max(round(total * 100 / top), 1)}
            for reason, total, n in sources
        ],
        "earned": earned,
        "spent": spent,
        "spent_share": round(spent * 100 / earned, 1) if earned else 0,
        "shop": sinks.get(R.PURCHASE, (0, 0)),
        "cases_spent": sinks.get(R.CASE, (0, 0)),
        "popular": [
            {"name": name, "kind": kinds.get(kind, kind), "n": n}
            for name, kind, n in UserItem.objects.values_list("item__name", "item__kind").annotate(n=Count("id")).order_by("-n", "item__name")[:8]
        ],
        "owned": UserItem.objects.count(),
        "on_sale": CosmeticItem.objects.filter(sold=True).count(),
        "rich": list(Wallet.objects.select_related("user").order_by("-balance")[:8]),
        "manual": list(BalanceLog.objects.filter(reason=R.MANUAL).select_related("wallet__user", "by")[:10]),
        "manual_total": BalanceLog.objects.filter(reason=R.MANUAL).aggregate(n=Count("id"), total=Sum("amount")),
    }


def cases():
    """Кейсы с итогами открытий. Считаем отдельным запросом по открытиям: соединение и с
    вещами, и с открытиями сразу перемножило бы строки и раздуло суммы."""
    opened = {
        row["case"]: row for row in CaseOpening.objects.values("case").annotate(
            n=Count("id"), free=Count("id", filter=Q(paid=0)), paid=Sum("paid"), refund=Sum("refund"),
        ).order_by()
    }
    rows = []
    for case in Case.objects.annotate(size=Count("items")):
        stats = opened.get(case.pk, {})
        rows.append({
            "case": case, "size": case.size, "opened": stats.get("n", 0), "free": stats.get("free", 0),
            "paid": stats.get("paid") or 0, "refund": stats.get("refund") or 0,
        })
    return rows


def rarity():
    """Что выпадало против заявленных шансов (cosmetics.cases.ODDS), по всем кейсам."""
    got = dict(CaseOpening.objects.values_list("rarity").annotate(n=Count("id")).order_by())
    total = sum(got.values())
    labels = dict(CosmeticItem.Rarity.choices)
    return {
        "total": total,
        "small": total < SMALL_SAMPLE,
        "rows": [
            {"label": labels[rarity], "n": got.get(rarity, 0), "share": round(got.get(rarity, 0) * 100 / total) if total else 0,
             "odds": odds}
            for rarity, odds in ODDS.items()
        ],
    }


def toggle_case(request, case, active):
    if not request.user.has_perm("cosmetics.change_case"):
        raise Refused("Для этого нужно право, которого у тебя нет.")
    if active and not case.items.exists():
        raise Refused("Сначала выбери вещи: пустой кейс открыть нельзя.")
    case.active = active
    case.save(update_fields=["active"])
    logged(request, case, "кейс открывается" if active else "кейс закрыт")
    return f"«{case.name}»: {'открывается' if active else 'закрыт'}"


def reconcile(request, apply):
    """Сверка кошельков с журналом — то же, что recount_balances. apply — привести кэш к журналу."""
    broken = mismatched()
    if apply:
        if not request.user.has_perm("economy.change_wallet"):
            raise Refused("Для этого нужно право, которого у тебя нет.")
        for wallet, journal in broken:
            logged(request, wallet, f"баланс приведён к журналу: было {wallet.balance}, стало {journal}")
            recount(wallet)
    return broken
