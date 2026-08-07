from django.urls import path

from . import views

urlpatterns = [
    path("moderation/", views.review_queue, name="review_queue"),
]
