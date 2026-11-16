from django.urls import path

from . import views

urlpatterns = [
    path("wall/", views.wall, name="wall"),
    path("wall/snapshot/", views.board_snapshot, name="wall_snapshot"),
    path("wall/owners/", views.board_owners, name="wall_owners"),
    path("wall/history/", views.board_history, name="wall_history"),
    path("wall/draw/", views.board_draw, name="wall_draw"),
    path("wall/undo/", views.board_undo, name="wall_undo"),
    # Координаты — в строке запроса: клиент спрашивает на каждый выбор клетки.
    path("wall/pixel/", views.pixel_card, name="wall_pixel"),
    path("wall/fit/", views.picture_fit, name="wall_fit"),
    path("wall/stamp/", views.picture_stamp, name="wall_stamp"),

    path("wall/fill/", views.area_fill, name="wall_fill"),
    path("wall/rollback/", views.area_rollback, name="wall_rollback"),
    path("wall/rollback/person/", views.person_rollback, name="wall_rollback_person"),
    path("wall/protect/", views.area_protect, name="wall_protect"),
    path("wall/unprotect/", views.area_unprotect, name="wall_unprotect"),
    path("wall/ban/", views.person_ban, name="wall_ban"),
    path("wall/bans/", views.person_bans, name="wall_bans"),
    path("wall/new/", views.board_new, name="wall_new"),
]
