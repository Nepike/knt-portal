from django.urls import path

from . import views

urlpatterns = [
    path("profile/wallet/", views.wallet, name="wallet"),
]
