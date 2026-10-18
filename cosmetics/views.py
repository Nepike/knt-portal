from urllib.parse import urlencode

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from economy.services import NotEnoughFunds, wallet_of

from .cases import STOP, AllCollected, CaseClosed, on_show, open_case, reel
from .models import Case, CosmeticItem, UserItem
from .services import NotOwned, equip, outfit, unequip
from .shop import DEFAULT_ORDER, ORDERS, AlreadyOwned, NotForSale, arrange, buy, marked, showcase


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


def _coins(user):
    # Кошелька может не быть вовсе — это ноль, а не повод заводить строку на просмотре.
    wallet = getattr(user, "wallet", None)
    return wallet.balance if wallet else 0


def _picked(data):
    """Порядок и галочка «мне по карману» из запроса. Незнакомое значение — умолчание,
    а не ошибка: адрес могли поправить руками."""
    order = data.get("order")
    return (order if order in ORDERS else DEFAULT_ORDER), data.get("afford") == "1"


def _query(order, affordable):
    """Хвост адреса с выбором человека — для ссылок, возврата после покупки и пересылки.
    Умолчания в адрес не идут: голый /shop/ открывается с ними и так."""
    picked = {}
    if order != DEFAULT_ORDER:
        picked["order"] = order
    if affordable:
        picked["afford"] = "1"
    return urlencode(picked)


def _shop_url(query):
    return f"{reverse('shop')}?{query}" if query else reverse("shop")


def shop(request):
    """Витрина полками. Порядок и галочка меняются без перезагрузки: htmx забирает одни
    полки, а адрес вьюха кладёт заголовком — ссылку с подбором можно переслать.

    Состояние каждой плитки («уже есть», «сколько не хватает») считаем здесь: в шаблоне
    оно выражается криво, а запросов не добавляет — обе величины уже на руках."""
    coins = _coins(request.user)
    order, affordable = _picked(request.GET)
    items = marked(request.user, coins)
    query = _query(order, affordable)
    context = {
        "coins": coins, "shelves": showcase(items, order, affordable),
        "order": order, "affordable": affordable, "query": query,
    }
    if request.headers.get("HX-Request"):
        response = render(request, "cosmetics/_shelves.html", context)
        response["HX-Push-Url"] = _shop_url(query)
        return response
    context["cases"] = on_show(request.user, coins)
    return render(request, "cosmetics/shop.html", context)


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

    Стрелки листают ту же полку, что и на странице, с тем же порядком и галочкой —
    поэтому выбор человека едет сюда в адресе.
    """
    item = get_object_or_404(CosmeticItem, pk=pk, sold=True)
    coins = _coins(request.user)
    order, affordable = _picked(request.GET)
    kin = [one for one in marked(request.user, coins) if one.kind == item.kind]
    shelf = arrange(kin, order, affordable)
    # Отбор считается заново на каждый запрос, и вещи на отобранной полке может уже не быть:
    # пока окно было открыто, её купили или потратили деньги в другой вкладке. Тогда листаем
    # полку целиком — стрелкам надо от чего-то отсчитывать.
    if item.pk not in {one.pk for one in shelf}:
        shelf = arrange(kin, order)
    at = [one.pk for one in shelf].index(item.pk)
    mine = shelf[at]
    on = outfit(request.user)
    return render(request, "cosmetics/_offer.html", {
        "item": item,
        "preview": PREVIEWS.get(item.kind, "cosmetics/preview/card.html"),
        "coins": coins,
        "owned": mine.owned,
        "short": mine.short,
        "before": shelf[at - 1] if at else None,
        "after": shelf[at + 1] if at + 1 < len(shelf) else None,
        "position": at + 1,
        "total": len(shelf),
        "order": order,
        "affordable": affordable,
        "query": _query(order, affordable),
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
    # Обратно в витрину с тем же подбором, что был: иначе после каждой покупки галочку
    # и порядок пришлось бы выставлять заново.
    return redirect(_shop_url(_query(*_picked(request.POST))))


def _shown_case(user, coins, pk):
    """Кейс с пометками витрины (cosmetics.cases.on_show) или None, если его на витрине нет."""
    return next((case for case in on_show(user, coins) if case.pk == pk), None)


def case_card(request, pk):
    """Что внутри кейса: вещи по ступеням от мифической, у ступени её шанс, что уже есть —
    помечено. Приезжает по htmx в то же окно, что и карточка товара."""
    coins = _coins(request.user)
    case = _shown_case(request.user, coins, pk)
    if case is None:
        raise Http404
    owned = set(UserItem.objects.filter(user=request.user).values_list("item_id", flat=True))
    items = sorted(case.items.all(), key=lambda item: item.name.casefold())
    groups = []
    for rarity, label, _, share in reversed(case.chances):
        group = [item for item in items if item.rarity == rarity]
        for item in group:
            item.owned = item.pk in owned
        groups.append((rarity, label, share, group))
    return render(request, "cosmetics/_case.html", {"case": case, "groups": groups, "coins": coins})


@require_POST
def case_open(request, pk):
    """Открыть кейс. По htmx — окно с рулеткой, без него — назад в витрину со словами.

    Кейсы под полками окно перерисовывает сразу: после первого, подарочного, открытия
    «бесплатно» обязано смениться ценой, иначе кнопка обещала бы подарок, а взяли бы деньги.
    """
    case = get_object_or_404(Case, pk=pk)
    opening, error = None, None
    try:
        opening = open_case(request.user, case)
    except NotEnoughFunds:
        error = f"Не хватает токенов на «{case.name}»"
    except AllCollected:
        error = f"Из «{case.name}» у тебя уже есть всё"
    except CaseClosed:
        error = f"«{case.name}» сейчас не открывается"

    if not request.headers.get("HX-Request"):
        if error:
            messages.error(request, error)
        elif opening.item:
            messages.success(request, f"Выпало: {opening.item.name}. Надеть можно в профиле")
        else:
            messages.success(request, f"Выпала {opening.get_rarity_display()}, но такие у тебя уже все — вернули {opening.refund}")
        return redirect("shop")

    # Кошелёк читаем заново: открытие меняло его другим экземпляром.
    coins = wallet_of(request.user).balance
    cases = on_show(request.user, coins)
    context = {"case": case, "error": error, "coins": coins, "cases": cases}
    if opening:
        context.update(
            opening=opening, reel=reel(list(case.items.all()), opening), stop=STOP,
            again=next((one for one in cases if one.pk == case.pk), None),
        )
    return render(request, "cosmetics/_roulette.html", context)
