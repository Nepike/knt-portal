from django.urls import path

from . import views

urlpatterns = [
    # Имя файла в хвосте — из него браузер берёт имя при сохранении, поэтому
    # Content-Disposition не нужен (через X-Accel-Redirect он ведёт себя непредсказуемо).
    path("f/<str:token>/<str:name>", views.download, name="file_download"),
    path("files/upload-url/", views.upload_url, name="upload_url"),
    # Многочастная загрузка: начать/продолжить, взять ссылки на части, собрать, бросить.
    path("files/upload/start/", views.upload_start, name="upload_start"),
    path("files/upload/parts/", views.upload_parts, name="upload_parts"),
    path("files/upload/finish/", views.upload_finish, name="upload_finish"),
    path("files/upload/abort/", views.upload_abort, name="upload_abort"),
    # Не /media/ — этот префикс в разработке занят раздачей локальных файлов.
    path("img/<str:token>/", views.media_image, name="media_image"),
    # Имя в хвосте — ради расширения: на него смотрят плееры и промежуточные кеши.
    path("hls/<str:token>/<str:name>", views.hls_piece, name="hls_piece"),
]
