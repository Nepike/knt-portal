from django.conf import settings
from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("about/", views.applicants, name="applicants"),
    path('applicants/', RedirectView.as_view(pattern_name='applicants', permanent=True)),
    path("contacts/", views.contacts, name="contacts"),
    path("support/", views.support, name="support"),
]

# Витрина полей и кнопок — она же стенд, на котором их и проверяют. Наружу не выставляем:
# на боевом сайте это страница без смысла, но с настоящими формами.
if settings.DEBUG:
    urlpatterns.append(path("demo/", views.demo, name="demo"))
