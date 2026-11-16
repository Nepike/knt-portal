from django.urls import path

from . import views

urlpatterns = [
    path("bookmarks/", views.bookmark_list, name="bookmark_list"),
    path("bookmarks/drop/<int:pk>/", views.bookmark_drop, name="bookmark_drop"),
    # Вид в адресе: кнопка в шапке одна на весь сайт.
    path("bookmarks/<str:kind>/<int:pk>/", views.bookmark_toggle, name="bookmark_toggle"),
]
