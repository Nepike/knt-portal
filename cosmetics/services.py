"""Выдача вещей и экипировка.

Всё, что меняет инвентарь, идёт через эти функции: правило «одна вещь на слот» держится
ограничением в базе, но снимать прежнюю приходится ДО того, как надета новая, — иначе
ограничение и сработает, отказом.
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


def worn(user, kind=CosmeticItem.Kind.AVATAR_FRAME):
    """Надетая вещь или None. Одним запросом вместе с самим предметом."""
    if not user or not user.is_authenticated:
        return None
    owned = UserItem.objects.filter(user=user, kind=kind, equipped=True).select_related("item").first()
    return owned.item if owned else None


def inventory(user):
    """Инвентарь блоками: сперва вид вещи, внутри — от обычной к редкой.

    Ранг считаем в запросе, а не в питоне: разметка группирует вещи подряд идущими
    (`regroup`), и порядок обязан приехать из базы уже правильным. По самой редкости
    сортировать нельзя — в базе это строка, и алфавит ставит legendary между epic и mythical.
    """
    rank = Case(
        *[When(item__rarity=value, then=step) for step, value in enumerate(CosmeticItem.RARITY_ORDER)],
        default=0, output_field=IntegerField(),
    )
    return (
        UserItem.objects.filter(user=user).select_related("item")
        .annotate(rank=rank).order_by("kind", "rank", "item__name")
    )
