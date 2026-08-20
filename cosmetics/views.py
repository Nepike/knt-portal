from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .models import CosmeticItem
from .services import NotOwned, equip, unequip


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
    """Снять надетое в слоте. Вид приходит из формы — слотов уже два."""
    kind = request.POST.get("kind")
    if kind not in CosmeticItem.Kind.values:
        kind = CosmeticItem.Kind.AVATAR_FRAME
    unequip(request.user, kind)
    messages.success(request, "Снято")
    return redirect("profile", pk=request.user.pk)
