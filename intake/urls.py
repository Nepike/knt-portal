from django.urls import path

from . import views

urlpatterns = [
    path("intake/spec/", views.spec, name="intake_spec"),
    path("intake/claim/", views.claim, name="intake_claim"),
    path("intake/plan/", views.plan, name="intake_plan"),
    path("intake/sign/", views.sign, name="intake_sign"),
    path("intake/commit/", views.commit, name="intake_commit"),
    path("intake/fail/", views.fail, name="intake_fail"),
]
