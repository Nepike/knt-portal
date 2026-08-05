from django.urls import path

from . import views

urlpatterns = [
    path("files/<int:pk>/", views.download, name="file_download"),
]
