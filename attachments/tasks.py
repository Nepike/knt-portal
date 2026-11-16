from io import StringIO

from celery import shared_task
from django.core.management import call_command

# Уборка обходит весь бакет — сотни запросов; общего потолка задач (минута) ей мало.
SWEEP_SOFT_LIMIT = 30 * 60
SWEEP_LIMIT = SWEEP_SOFT_LIMIT + 5 * 60


@shared_task(soft_time_limit=SWEEP_SOFT_LIMIT, time_limit=SWEEP_LIMIT)
def sweep_storage(days=1):
    """Ночная уборка хранилища: `clean_uploads --apply` — та же команда, что руками. Вывод —
    ответом задачи в лог воркера: уборка удаляет файлы, молчать ей нельзя."""
    out = StringIO()
    call_command("clean_uploads", "--apply", f"--days={days}", stdout=out)
    return out.getvalue().strip()
