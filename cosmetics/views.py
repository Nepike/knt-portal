from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from economy.services import NotEnoughFunds

from .models import CosmeticItem, UserItem
from .services import NotOwned, equip, outfit, unequip
from .shop import AlreadyOwned, NotForSale, buy, on_sale


@require_POST
def item_equip(request, pk):
    item = get_object_or_404(CosmeticItem, pk=pk)
    try:
        equip(request.user, item)
    except NotOwned:
        # Не 403: чужая вещь для человека просто не существует, и объяснять,
        # что она есть у кого-то другого, незачем.
        messages.error(request, "Такой вещи у тебя нет")
    else:
        messages.success(request, f"Надето: {item.name}")
    return redirect("profile", pk=request.user.pk)


@require_POST
def item_unequip(request):
    """Снять надетое в слоте. Вид приходит из формы — слотов уже три.

    Неизвестный вид — значит, запрос пришёл не с нашей страницы: подставлять вместо него
    рамку нельзя, человек снял бы не то. Молча уходим в профиль, как и session_end.
    """
    kind = request.POST.get("kind")
    if kind not in CosmeticItem.Kind.values:
        return redirect("profile", pk=request.user.pk)
    if unequip(request.user, kind):
        messages.success(request, "Снято")
    return redirect("profile", pk=request.user.pk)


def shop(request):
    """Витрина. Состояние каждой плитки считаем здесь: «уже есть» и «сколько не хватает»
    в шаблоне выражаются криво, а запросов это не добавляет — обе величины уже на руках."""
    # Кошелька может не быть вовсе — это ноль, а не повод заводить строку на просмотре.
    wallet = getattr(request.user, "wallet", None)
    coins = wallet.balance if wallet else 0
    owned = set(UserItem.objects.filter(user=request.user).values_list("item_id", flat=True))

    items = list(on_sale())
    for item in items:
        item.owned = item.pk in owned
        item.short = max(item.cost - coins, 0)

    return render(request, "cosmetics/shop.html", {"items": items, "coins": coins})


# Свой предпросмотр на каждый вид вещи: рамку и шапку надо видеть вблизи, а фон —
# картой всей страницы, потому что он и кроется по всей странице. Один общий шаблон
# обслуживал бы кого-то плохо. Новому виду — своя строка и свой файл в preview/.
PREVIEWS = {
    CosmeticItem.Kind.AVATAR_FRAME: "cosmetics/preview/card.html",
    CosmeticItem.Kind.PROFILE_HEADER: "cosmetics/preview/card.html",
    CosmeticItem.Kind.PROFILE_BACKGROUND: "cosmetics/preview/page.html",
}


def item_card(request, pk):
    """Карточка товара для окна магазина: предпросмотр, цена, кнопка.

    Покупаемая вещь занимает свой слот, остальные показываем нынешние. Иначе человек
    не увидит, как покупка уживётся с тем, что у него уже надето.
    """
    item = get_object_or_404(CosmeticItem, pk=pk, sold=True)
    wallet = getattr(request.user, "wallet", None)
    coins = wallet.balance if wallet else 0
    on = outfit(request.user)
    return render(request, "cosmetics/_offer.html", {
        "item": item,
        "preview": PREVIEWS.get(item.kind, "cosmetics/preview/card.html"),
        "coins": coins,
        "owned": UserItem.objects.filter(user=request.user, item=item).exists(),
        "short": max(item.cost - coins, 0),
        "frame": item if item.kind == CosmeticItem.Kind.AVATAR_FRAME else on.get(CosmeticItem.Kind.AVATAR_FRAME),
        "header": item if item.kind == CosmeticItem.Kind.PROFILE_HEADER else on.get(CosmeticItem.Kind.PROFILE_HEADER),
        "background": item if item.kind == CosmeticItem.Kind.PROFILE_BACKGROUND else on.get(CosmeticItem.Kind.PROFILE_BACKGROUND),
    })


@require_POST
def item_buy(request, pk):
    item = get_object_or_404(CosmeticItem, pk=pk)
    try:
        buy(request.user, item)
    except NotEnoughFunds:
        messages.error(request, f"Не хватает токенов на «{item.name}»")
    except AlreadyOwned:
        messages.error(request, f"«{item.name}» уже в инвентаре")
    except NotForSale:
        messages.error(request, f"«{item.name}» не продаётся")
    else:
        # Не надеваем: покупают и про запас, а подмена надетого без спроса читается
        # как поломка. Поэтому и ведём в инвентарь — показать, куда вещь легла.
        messages.success(request, f"Куплено: {item.name}. Надеть можно в профиле")
    return redirect("shop")
