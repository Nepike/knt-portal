"""События доски в channel layer. Наружу летят сами данные: клетка у всех одна и та же."""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from users.models import User


def board_group(board_id):
    return f"wall.{board_id}"


def notify_area(board_id, placements):
    """Пачка клеток одним сообщением: [x, y, цвет, автор] и имена авторов — для подсказки «кто»."""
    if not placements:
        return
    authors = {placement.user_id for placement in placements if placement.user_id}
    people = {user.pk: user.full_name for user in User.objects.filter(pk__in=authors)}
    _publish(board_group(board_id), {
        "type": "wall.area",
        "id": max(placement.pk for placement in placements),
        "pixels": [[placement.x, placement.y, placement.color, placement.user_id or 0] for placement in placements],
        "people": {str(pk): name for pk, name in people.items()},
    })


def notify_restore(board, cells):
    """Отмена: клетки вернулись к прежнему виду — [x, y, цвет, автор, секунды от открытия доски].
    Номера нет: события удалены, и последний номер журнала мог стать меньше того, что видел
    клиент. Такое сообщение клиент кладёт без сверки номера."""
    if not cells:
        return
    authors = {owner for _, _, _, owner, _ in cells if owner}
    people = {user.pk: user.full_name for user in User.objects.filter(pk__in=authors)}
    _publish(board_group(board.pk), {
        "type": "wall.area",
        "restore": True,
        "pixels": [
            [x, y, color, owner or 0, max(0, int((placed - board.created).total_seconds()))]
            for x, y, color, owner, placed in cells
        ],
        "people": {str(pk): name for pk, name in people.items()},
    })


def _publish(group, payload):
    layer = get_channel_layer()
    if layer is None:  # слой не настроен — например, в management-команде
        return
    async_to_sync(layer.group_send)(group, payload)
