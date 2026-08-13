"""Публикация событий доски в channel layer.

В отличие от чата наружу летят сами данные, а не «в чате N что-то произошло».
Причина в том, что у пикселя нет разметки, которая была бы у каждого своей: есть
координата и цвет, одинаковые для всех. Заставлять клиента ходить за ними по HTTP
значило бы делать запрос на каждый чужой мазок.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def board_group(board_id):
    return f"wall.{board_id}"


def notify_pixel(placement):
    _publish(board_group(placement.board_id), {
        "type": "wall.pixel",
        "id": placement.pk,
        "x": placement.x,
        "y": placement.y,
        "color": placement.color,
    })


def notify_area(board_id, placements):
    """Пачка клеток одним сообщением — работа модератора.

    Заливкой и откатом за раз меняются тысячи клеток. Отдельным событием на каждую
    мы бы завалили сокет каждому, кто в этот момент смотрит на доску.
    """
    if not placements:
        return
    _publish(board_group(board_id), {
        "type": "wall.area",
        "id": max(placement.pk for placement in placements),
        "pixels": [[placement.x, placement.y, placement.color] for placement in placements],
    })


def _publish(group, payload):
    layer = get_channel_layer()
    if layer is None:  # слой не настроен — например, в management-команде
        return
    async_to_sync(layer.group_send)(group, payload)
