from django.urls import path

from . import views

urlpatterns = [
    path("lectures/", views.playlist_list, name="playlist_list"),
    path("lectures/new/", views.playlist_edit, name="playlist_new"),
    path("lectures/check/", views.check, name="hls_check"),
    path("lectures/<int:pk>/", views.playlist_detail, name="playlist_detail"),
    path("lectures/<int:pk>/edit/", views.playlist_edit, name="playlist_edit"),
    path("lectures/<int:pk>/add/", views.lecture_add, name="lecture_add"),
    path("lectures/<int:pk>/review/", views.playlist_review, name="playlist_review"),
]
