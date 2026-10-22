from django.urls import path

from . import agent, views

# Страницы людей — «print_*», ручки агента — «printagent_*»: при закрытии сайта открыты
# только вторые (core/lockdown.py), страницы людей закрываются вместе со всем.
urlpatterns = [
    path("print/", views.home, name="print_home"),
    path("print/upload-url/", views.sign, name="print_sign"),
    path("print/upload/", views.upload, name="print_upload"),
    path("print/<uuid:uuid>/", views.job, name="print_job"),
    path("print/<uuid:uuid>/status/", views.status, name="print_status"),
    path("print/<uuid:uuid>/print/", views.submit, name="print_submit"),
    path("print/<uuid:uuid>/cancel/", views.cancel, name="print_cancel"),
    path("print/<uuid:uuid>/remove/", views.remove, name="print_remove"),
    path("print/<uuid:uuid>/again/", views.again, name="print_again"),
    path("printing/agent/config/", agent.config, name="printagent_config"),
    path("printing/agent/claim/", agent.claim, name="printagent_claim"),
    path("printing/agent/analyzed/", agent.analyzed, name="printagent_analyzed"),
    path("printing/agent/commit/", agent.commit, name="printagent_commit"),
    path("printing/agent/sent/", agent.sent, name="printagent_sent"),
    path("printing/agent/printed/", agent.printed, name="printagent_printed"),
    path("printing/agent/fail/", agent.fail, name="printagent_fail"),
    path("printing/agent/alive/", agent.alive, name="printagent_alive"),
    path("printing/agent/release/", agent.release, name="printagent_release"),
    path("printing/agent/status/", agent.status, name="printagent_status"),
]
