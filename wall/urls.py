from django.urls import path

from . import views

urlpatterns = [
    path("wall/", views.wall, name="wall"),
    path("wall/snapshot/", views.board_snapshot, name="wall_snapshot"),
    path("wall/version/", views.board_version, name="wall_version"),
    path("wall/history/", views.board_history, name="wall_history"),
    path("wall/paint/", views.pixel_paint, name="wall_paint"),
    path("wall/erase/", views.pixel_erase, name="wall_erase"),
    path("wall/reroll/", views.color_reroll, name="wall_reroll"),
    # Координаты в строке запроса, а не в пути: этот адрес клиент дёргает на каждый
    # клик, и дописать к нему «?x=…» проще, чем собирать путь в JS.
    path("wall/pixel/", views.pixel_card, name="wall_pixel"),

    path("wall/fill/", views.area_fill, name="wall_fill"),
    path("wall/rollback/", views.area_rollback, name="wall_rollback"),
    path("wall/protect/", views.area_protect, name="wall_protect"),
    path("wall/unprotect/", views.area_unprotect, name="wall_unprotect"),
    path("wall/ban/", views.person_ban, name="wall_ban"),
    path("wall/new/", views.board_new, name="wall_board_new"),
]
