from django.urls import path

from . import views

urlpatterns = [
    path("profile/items/<int:pk>/equip/", views.item_equip, name="item_equip"),
    path("profile/items/unequip/", views.item_unequip, name="item_unequip"),
]
