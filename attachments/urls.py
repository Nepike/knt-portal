from django.urls import path

from . import views

urlpatterns = [
    # Имя файла в хвосте — из него браузер берёт имя при сохранении, поэтому
    # Content-Disposition не нужен (через X-Accel-Redirect он ведёт себя непредсказуемо).
    path("f/<str:token>/<str:name>", views.download, name="file_download"),
    path("files/upload-url/", views.upload_url, name="upload_url"),
    # Не /media/ — этот префикс в разработке занят раздачей локальных файлов.
    path("img/<str:token>/", views.media_image, name="media_image"),
]
