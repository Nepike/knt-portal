"""Выдача вещей и экипировка. «Одна вещь на слот» держит ограничение в базе — поэтому прежнюю
снимаем ДО того, как надета новая.
"""

from django.db import transaction
from django.db.models import Case, IntegerField, When

from .models import CosmeticItem, UserItem


class NotOwned(Exception):
    """Надеть можно только своё."""


def grant(user, item):
    """Выдать вещь. Уже имеющуюся не задваиваем — возвращаем ту же запись."""
    owned, _ = UserItem.objects.get_or_create(user=user, item=item)
    return owned


@transaction.atomic
def equip(user, item):
    """Надеть. Прежняя вещь того же вида снимается: слот один."""
    owned = UserItem.objects.filter(user=user, item=item).first()
    if owned is None:
        raise NotOwned(f"{user} не владеет предметом «{item.name}»")

    UserItem.objects.filter(user=user, kind=item.kind, equipped=True).exclude(pk=owned.pk).update(equipped=False)
    if not owned.equipped:
        owned.equipped = True
        owned.save(update_fields=["equipped"])
    return owned


def unequip(user, kind=CosmeticItem.Kind.AVATAR_FRAME):
    """Снять то, что надето в этом слоте."""
    return UserItem.objects.filter(user=user, kind=kind, equipped=True).update(equipped=False)


def outfit(user):
    """Всё надетое разом: {вид: предмет}. Одним запросом, сколько бы слотов ни было."""
    if not user or not user.is_authenticated:
        return {}
    rows = UserItem.objects.filter(user=user, equipped=True).select_related("item")
    return {row.kind: row.item for row in rows}


def worn(user, kind=CosmeticItem.Kind.AVATAR_FRAME):
    """Надетая вещь одного вида или None."""
    return outfit(user).get(kind)


def rarity_rank(field="rarity"):
    """Ступень числом — для сортировки (в базе редкость строкой)."""
    return Case(
        *[When(**{field: value}, then=step) for step, value in enumerate(CosmeticItem.RARITY_ORDER)],
        default=0, output_field=IntegerField(),
    )


def inventory(user):
    """Инвентарь блоками: вид, внутри — от обычной к редкой. Порядок — из базы: разметка
    группирует подряд идущие (`regroup`)."""
    return (
        UserItem.objects.filter(user=user).select_related("item")
        .annotate(rank=rarity_rank("item__rarity")).order_by("kind", "rank", "item__name")
    )
