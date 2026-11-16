# Celery поднимается вместе с Django, иначе @shared_task не попадут в реестр.
from .celery import app as celery_app

__all__ = ("celery_app",)
