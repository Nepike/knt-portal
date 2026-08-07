from django.urls import path

from . import views

urlpatterns = [
    path("library/", views.book_list, name="book_list"),
    path("library/new/", views.book_edit, name="book_new"),
    path("library/<int:pk>/", views.book_detail, name="book_detail"),
    path("library/<int:pk>/edit/", views.book_edit, name="book_edit"),
    path("library/<int:pk>/review/", views.book_review, name="book_review"),
    path("library/<int:pk>/delete/", views.book_delete, name="book_delete"),
]
