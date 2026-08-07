"""Сборка Celery — очереди фоновых задач.

Воркер запускается отдельным процессом, и Django в нём никто не поднимет:
переменную окружения и загрузку настроек делаем здесь сами.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knt.settings.dev")

app = Celery("knt")
# Настройки очереди живут в общих settings с префиксом CELERY_ — отдельного файла конфига нет.
app.config_from_object("django.conf:settings", namespace="CELERY")
# Задачи ищутся сами: tasks.py в каждом приложении из INSTALLED_APPS.
app.autodiscover_tasks()
