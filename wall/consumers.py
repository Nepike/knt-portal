from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .events import board_group
from .models import Board


class WallConsumer(AsyncJsonWebsocketConsumer):
    """Один сокет на открытую страницу доски.

    Группа заведена по доске, а не одна на всех: архивные доски событий не получают,
    и когда семестр сменится, старая страница не будет ловить чужие пиксели.
    """

    async def connect(self):
        if not self.scope["user"].is_authenticated:
            await self.close()
            return
        board_id = await self.current_board()
        if board_id is None:
            await self.close()
            return
        self.group = board_group(board_id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    @database_sync_to_async
    def current_board(self):
        board = Board.current()
        return board.pk if board else None

    async def wall_pixel(self, event):
        await self.send_json({key: event[key] for key in ("id", "x", "y", "color")})

    async def wall_area(self, event):
        await self.send_json({key: event[key] for key in ("id", "pixels")})
