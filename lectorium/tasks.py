from celery import shared_task

from attachments.storage import drop_prefix


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def drop_lecture_files(prefix):
    """Убрать из хранилища набор удалённой лекции.

    Не в запросе: у двухчасовой лекции это около 2400 ключей, то есть несколько
    запросов в чужую сеть даже пакетами. Повторы на случай, если бакет отвечает
    не с первого раза, — иначе сироты останутся навсегда.
    """
    return drop_prefix(prefix)
