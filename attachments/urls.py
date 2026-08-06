from django.urls import path

from . import views

urlpatterns = [
    path("files/<int:pk>/", views.download, name="file_download"),
    path("files/upload-url/", views.upload_url, name="upload_url"),
]
