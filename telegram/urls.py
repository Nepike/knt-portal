from django.urls import path

from . import views

# «analytics_*» — машина со своим токеном: при закрытии сайта открыта (core/lockdown.py).
urlpatterns = [
    path("inbox/analytics/", views.analytics_inbox, name="analytics_inbox"),
]
