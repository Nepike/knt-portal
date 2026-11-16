import posixpath

from celery import shared_task

from attachments.storage import drop_prefix


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def drop_source(key):
    """Убрать сырьё, из которого всё испекли (вчетверо тяжелее набора): папку целиком. Кроме
    случая, когда тот же ключ сдан и приложением к материалу (`File`) — спрашиваем базу."""
    from attachments.models import File

    if File.objects.filter(file=key).exists():
        return 0
    return drop_prefix(posixpath.dirname(key))


@shared_task
def watch_bakeries():
    """Раз в пять минут: записи стоят, а пекарни молчат (intake.health.watch)."""
    from .health import watch

    return watch()
