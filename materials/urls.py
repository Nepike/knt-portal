from django.urls import path

from . import views

urlpatterns = [
    path("materials/", views.material_list, name="material_list"),
    path("materials/new/", views.material_edit, name="material_new"),
    path("materials/<int:pk>/", views.material_detail, name="material_detail"),
    path("materials/<int:pk>/edit/", views.material_edit, name="material_edit"),
    path("materials/<int:pk>/review/", views.material_review, name="material_review"),
    path("materials/<int:pk>/delete/", views.material_delete, name="material_delete"),
    path("materials/<int:pk>/comment/", views.comment_add, name="comment_add"),
    path("materials/comments/<int:pk>/edit/", views.comment_edit, name="comment_edit"),
    path("materials/comments/<int:pk>/delete/", views.comment_delete, name="comment_delete"),
    path("materials/comments/<int:pk>/like/", views.comment_vote, {"vote": "like"}, name="comment_like"),
    path("materials/comments/<int:pk>/dislike/", views.comment_vote, {"vote": "dislike"}, name="comment_dislike"),
]
