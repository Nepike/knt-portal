from django.conf import settings
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("about/", views.applicants, name="applicants"),
    path("contacts/", views.contacts, name="contacts"),
    path("support/", views.support, name="support"),
]

# Витрина полей и кнопок — она же стенд, на котором их и проверяют. Наружу не выставляем:
# на боевом сайте это страница без смысла, но с настоящими формами.
if settings.DEBUG:
    urlpatterns.append(path("demo/", views.demo, name="demo"))
