from django.urls import path

from . import views

urlpatterns = [
    path("bookmarks/", views.bookmark_list, name="bookmark_list"),
    path("bookmarks/drop/<int:pk>/", views.bookmark_drop, name="bookmark_drop"),
    # Вид в адресе, а не отдельная ручка на каждый: кнопка в шапке одна на весь сайт,
    # и знать, на какой она сейчас странице, ей незачем.
    path("bookmarks/<str:kind>/<int:pk>/", views.bookmark_toggle, name="bookmark_toggle"),
]
