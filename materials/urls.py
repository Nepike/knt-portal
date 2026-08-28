from django.urls import path

from . import views

urlpatterns = [
    path("materials/", views.material_list, name="material_list"),
    path("materials/new/", views.material_edit, name="material_new"),
    path("materials/<int:pk>/", views.material_detail, name="material_detail"),
    path("materials/<int:pk>/edit/", views.material_edit, name="material_edit"),
    path("materials/<int:pk>/review/", views.material_review, name="material_review"),
    path("materials/<int:pk>/delete/", views.material_delete, name="material_delete"),
]
