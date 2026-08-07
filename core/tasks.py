from celery import shared_task

from .mail import deliver


@shared_task(ignore_result=False)
def ping(word="pong"):
    """Проверка живости связки «сайт → Redis → воркер»: manage.py celery_check.

    ignore_result=False — единственная задача, ответ которой нам действительно нужен.
    """
    return word


# smtplib.SMTPException — наследник OSError, так что сюда попадают и обрыв сети,
# и отказ сервера. Пауза между попытками растёт, чтобы не долбить лежащий gmail.
@shared_task(autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def send_email(payload):
    """Письмо, собранное в веб-процессе (core.mail.pack). Отправляет воркер — своим
    соединением с SMTP, и он же повторяет попытку, если письмо не ушло."""
    deliver(payload)
