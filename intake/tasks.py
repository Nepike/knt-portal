import posixpath

from celery import shared_task

from attachments.storage import drop_prefix


@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def drop_source(key):
    """Убрать сырьё, из которого уже всё испекли.

    Гигабайты, лежащие мёртвым грузом: у двухчасовой лекции исходник весит вчетверо
    больше готового набора. Снимаем папку целиком — ключ прямой загрузки лежит
    в своей (`uploads/<uuid>/имя`), и кроме него там ничего нет.
    """
    return drop_prefix(posixpath.dirname(key))
