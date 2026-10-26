"""«Система» в Пульсе: состояние служб, расписание, сбои и ручные запуски."""

import logging

from celery.schedules import crontab
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max, Q
from django.utils import timezone

from core import backup, runs
from core.lockdown import PASS
from core.models import Failure, Lockdown
from core.tasks import backup_database, ping
from intake.models import MediaJob
from knt.celery import app as celery_app
from printing.models import Printer

from .access import logged
from .people import Refused

logger = logging.getLogger(__name__)

# Как назвать задачи расписания. Новая задача без подписи тоже покажется — своим ключом,
# а тест (pulse.tests) напомнит дать ей человеческое имя.
LABELS = {
    "backup-database": "Бэкап базы",
    "sweep-printing": "Уборка печати",
    "sweep-journals": "Уборка журналов",
    "sweep-storage": "Уборка бакета",
    "watch-printers": "Присмотр за принтерами",
}
PING_WAIT = 5  # секунд ждём ответа воркера; дольше держать запрос незачем


def _when(schedule):
    if isinstance(schedule, crontab):
        hours, minutes = sorted(schedule.hour), sorted(schedule.minute)
        if len(hours) == 1 and len(minutes) == 1:
            return f"каждый день в {hours[0]}:{minutes[0]:02d}"
        return str(schedule)
    seconds = int(getattr(schedule, "total_seconds", lambda: schedule)())
    return "каждую минуту" if seconds == 60 else f"каждые {seconds} с"


def schedule():
    rows = []
    for key, entry in settings.CELERY_BEAT_SCHEDULE.items():
        rows.append({
            "label": LABELS.get(key, key), "when": _when(entry["schedule"]), "last": runs.last(entry["task"]),
        })
    return rows


def redis_memory():
    """(занято, потолок) у Redis или None — в разработке кэш в памяти процесса."""
    try:
        info = cache._cache.get_client().info("memory")
    except Exception:  # noqa: BLE001 — не Redis или он не отвечает: так и показываем
        return None
    return info.get("used_memory_human"), info.get("maxmemory_human")


def services(now=None):
    now = now or timezone.now()
    jobs = MediaJob.objects.aggregate(
        waiting=Count("id", filter=Q(status=MediaJob.Status.WAITING)),
        baking=Count("id", filter=Q(status=MediaJob.Status.BAKING)),
        done=Count("id", filter=Q(status=MediaJob.Status.DONE)),
        failed=Count("id", filter=Q(status=MediaJob.Status.FAILED)),
        claimed=Max("claimed_at"),
    )
    mail = Failure.objects.filter(source=Failure.Source.MAIL).order_by("-last_at").first()
    return {
        "printers": list(Printer.objects.filter(active=True)),
        "bakery": jobs,
        "redis": redis_memory(),
        "mail": mail,
        "backup": runs.last(backup_database.name),
        "lockdown": Lockdown.load(),
        "version": settings.SITE_VERSION,
    }


def failures(limit=20):
    return list(Failure.objects.all()[:limit])


def backups():
    """Копии базы в хранилище, свежие сверху. Читает бакет — поэтому по кнопке."""
    return [(day, key, size) for day, (key, size) in sorted(backup.stored().items(), reverse=True)]


def queue_alive():
    """Воркер ответил на ping за PING_WAIT секунд.

    Соединение с брокером — одной попыткой и до отправки: сама по себе отправка при лежащем
    Redis переподключается до сотни раз, и страница висела бы минутами."""
    try:
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=0)
            result = ping.apply_async(args=["pong"], connection=connection, retry=False)
        return result.get(timeout=PING_WAIT) == "pong"
    except Exception:  # noqa: BLE001 — нет брокера, нет воркера, истекло ожидание: всё это «не ответил»
        logger.warning("Очередь не ответила на проверку из Пульса", exc_info=True)
        return False


def set_lockdown(request, active):
    if not request.user.has_perm("core.change_lockdown"):
        raise Refused("Для этого нужно право, которого у тебя нет.")
    if active and not request.user.has_perm(PASS):
        raise Refused("Сначала дай себе «Проход при закрытии» — иначе закрытый сайт не пустит и тебя.")
    lock = Lockdown.load()
    lock.active = active
    lock.save()
    logged(request, lock, "сайт закрыт заслоном" if active else "заслон снят")
    return "Сайт закрыт заслоном" if active else "Заслон снят, сайт открыт"


def backup_now(request):
    if not request.user.is_superuser:
        raise Refused("Бэкап вне расписания запускает только суперпользователь.")
    backup_database.delay()
    logger.info("Бэкап вне расписания запросил %s", request.user.email)
    return "Бэкап поставлен в очередь — итог появится в расписании"
