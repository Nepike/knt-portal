from django.urls import path

from . import views

urlpatterns = [
    path("pulse/", views.home, name="pulse_home"),
    path("pulse/feed/", views.feed_page, name="pulse_feed"),
    path("pulse/remove/<str:kind>/<int:pk>/", views.remove, name="pulse_remove"),
    path("pulse/unpublish/<str:kind>/<int:pk>/", views.unpublish, name="pulse_unpublish"),
    path("pulse/people/", views.people_page, name="pulse_people"),
    path("pulse/people/<int:pk>/", views.person_page, name="pulse_person"),
    path("pulse/people/<int:pk>/printing/", views.person_printing, name="pulse_person_printing"),
    path("pulse/people/<int:pk>/wall/", views.person_wall, name="pulse_person_wall"),
    path("pulse/people/<int:pk>/active/", views.person_active, name="pulse_person_active"),
    path("pulse/people/<int:pk>/right/", views.person_right, name="pulse_person_right"),
    path("pulse/people/<int:pk>/sessions/", views.person_sessions, name="pulse_person_sessions"),
    path("pulse/people/<int:pk>/tokens/", views.person_tokens, name="pulse_person_tokens"),
    path("pulse/people/<int:pk>/password/", views.person_password, name="pulse_person_password"),
    path("pulse/people/<int:pk>/session/", views.person_session, name="pulse_person_session"),
    path("pulse/people/<int:pk>/gift/", views.person_gift, name="pulse_person_gift"),
    path("pulse/tokens/", views.tokens_page, name="pulse_tokens"),
    path("pulse/tokens/cases/<int:pk>/", views.case_toggle, name="pulse_case_toggle"),
    path("pulse/tokens/reconcile/", views.reconcile, name="pulse_reconcile"),
    path("pulse/wall/", views.wall_page, name="pulse_wall"),
    path("pulse/wall/unban/<int:pk>/", views.wall_unban, name="pulse_wall_unban"),
    path("pulse/wall/unprotect/<int:pk>/", views.wall_unprotect, name="pulse_wall_unprotect"),
    path("pulse/system/", views.system_page, name="pulse_system"),
    path("pulse/system/lockdown/", views.system_lockdown, name="pulse_system_lockdown"),
    path("pulse/system/backup/", views.system_backup, name="pulse_system_backup"),
    path("pulse/system/backups/", views.system_backups, name="pulse_system_backups"),
    path("pulse/system/queue/", views.system_queue, name="pulse_system_queue"),
]
