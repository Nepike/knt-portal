from celery import shared_task

from attachments.storage import drop_prefix


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def drop_lecture_files(prefix):
    """Убрать набор удалённой лекции: ~2400 ключей — в очередь, с повторами."""
    return drop_prefix(prefix)
