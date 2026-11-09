from django.urls import path

from . import views

urlpatterns = [
    path("notifications/", views.notification_list, name="notification_list"),
    path("notifications/panel/", views.notification_panel, name="notification_panel"),
    path("notifications/badge/", views.notification_badge, name="notification_badge"),
    path("notifications/read/", views.notification_read_all, name="notification_read_all"),
    path("notifications/<int:pk>/", views.notification_open, name="notification_open"),
    path("announcements/<int:pk>/", views.announcement_detail, name="announcement_detail"),
]
