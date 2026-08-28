from io import StringIO

from celery import shared_task
from django.core.management import call_command

# Уборка обходит ВЁСЬ бакет: перечислить папки, спросить у каждой возраст, у брошенных
# многочастных — список. Это сотни запросов в чужую сеть, и общий потолок задач (минута)
# ей мал по построению. Полчаса — с запасом на медленный день у Cloudflare.
SWEEP_SOFT_LIMIT = 30 * 60
SWEEP_LIMIT = SWEEP_SOFT_LIMIT + 5 * 60


@shared_task(soft_time_limit=SWEEP_SOFT_LIMIT, time_limit=SWEEP_LIMIT)
def sweep_storage(days=1):
    """Ночная уборка хранилища: `clean_uploads --apply`.

    Через команду, а не отдельной копией логики: руками её запускают ровно так же,
    и разъехаться этим двум способам нельзя — бакет один.

    Вывод забираем себе и печатаем ответом задачи: он уходит в лог воркера, и по нему
    видно, что и когда снесли. Без этого уборка была бы молчаливой, а она удаляет файлы.
    """
    out = StringIO()
    call_command("clean_uploads", "--apply", f"--days={days}", stdout=out)
    return out.getvalue().strip()
