# Celery поднимается вместе с Django: без этого @shared_task не к чему привязаться,
# и задачи из приложений просто не попадут в реестр.
from .celery import app as celery_app

__all__ = ("celery_app",)
