from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .mail import deliver, report_failure

MAIL_RETRIES = 3
# Журналы входов и сбоев — квартал.
JOURNAL_DAYS = 90
# Обращения — год: сезон поступления должен целиком попасть в выборку для весов спама.
INQUIRY_DAYS = 365
# Общий потолок задач — минута, а дамп растёт вместе с базой.
BACKUP_SOFT_LIMIT = 15 * 60
BACKUP_LIMIT = BACKUP_SOFT_LIMIT + 5 * 60


@shared_task(ignore_result=False)
def ping(word="pong"):
    """Проверка связки сайт → Redis → воркер (manage.py celery_check). Единственная задача с нужным ответом."""
    return word


# SMTPException — наследник OSError: сюда попадают и обрыв, и отказ сервера.
@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": MAIL_RETRIES})
def send_email(self, payload):
    """Письмо, собранное в веб-процессе (core.mail.pack); воркер отправляет и повторяет."""
    try:
        deliver(payload)
    except Exception as error:
        # Повторяют только OSError и только пока попытки не вышли — всё прочее уже отказ.
        if not isinstance(error, OSError) or self.request.retries >= MAIL_RETRIES:
            report_failure(payload, error)
        raise


@shared_task(soft_time_limit=BACKUP_SOFT_LIMIT, time_limit=BACKUP_LIMIT)
def backup_database():
    """Ночной бэкап базы (core/backup.py). Итог — в лог воркера, сбой — в журнал сбоев."""
    # Импорт здесь: задачи читаются при старте раньше моделей.
    from . import backup

    key, size = backup.make()
    gone = backup.prune()
    return f"{key}: {size} байт, старых снято: {len(gone)}"


@shared_task
def sweep_journals():
    """Снять старое из журналов, уведомлений и обращений, а с ними просроченные сессии:
    Django их сам не удаляет, а с сессией лежит строка «моих устройств»."""
    from django.contrib.sessions.models import Session

    from notifications.models import Announcement, Notification
    from users.models import LoginEvent

    from .models import Failure, Inquiry

    now = timezone.now()
    edge = now - timedelta(days=JOURNAL_DAYS)
    logins, _ = LoginEvent.objects.filter(created__lt=edge).delete()
    failures, _ = Failure.objects.filter(last_at__lt=edge).delete()
    asked, _ = Inquiry.objects.filter(created__lt=now - timedelta(days=INQUIRY_DAYS)).delete()
    notices, _ = Notification.objects.filter(created__lt=edge).delete()
    Announcement.objects.filter(created__lt=edge).delete()  # его строки ушли строкой выше
    _, gone = Session.objects.filter(expire_date__lt=now).delete()
    return (f"входов снято: {logins}, сбоев: {failures}, обращений: {asked}, уведомлений: {notices}, "
            f"сессий: {gone.get('sessions.Session', 0)}")
