from django.urls import path

from . import views

urlpatterns = [
    # Вид владельца в адресе: по номеру одному не понять, материал это или лекция,
    # а нумерация у них своя.
    path("comments/<str:kind>/<int:pk>/add/", views.comment_add, name="comment_add"),
    path("comments/<int:pk>/edit/", views.comment_edit, name="comment_edit"),
    path("comments/<int:pk>/delete/", views.comment_delete, name="comment_delete"),
    path("comments/<int:pk>/like/", views.comment_vote, {"vote": "like"}, name="comment_like"),
    path("comments/<int:pk>/dislike/", views.comment_vote, {"vote": "dislike"}, name="comment_dislike"),
]
