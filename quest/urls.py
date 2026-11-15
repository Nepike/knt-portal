from django.urls import path

from . import views

urlpatterns = [
    path("countdown/", views.countdown, name="countdown"),
    path("countdown/ask/", views.ask, name="countdown_ask"),
    path("party/", views.party, name="party"),
]
