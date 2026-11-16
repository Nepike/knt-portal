"""Celery. Воркер — отдельный процесс, поэтому настройки Django грузим здесь."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knt.settings.dev")

app = Celery("knt")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
