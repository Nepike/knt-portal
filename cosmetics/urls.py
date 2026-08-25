from django.urls import path

from . import views

urlpatterns = [
    path("profile/items/<int:pk>/equip/", views.item_equip, name="item_equip"),
    path("profile/items/unequip/", views.item_unequip, name="item_unequip"),
    path("shop/", views.shop, name="shop"),
    path("shop/<int:pk>/", views.item_card, name="item_card"),
    path("shop/<int:pk>/buy/", views.item_buy, name="item_buy"),
]
