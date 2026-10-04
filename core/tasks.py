from celery import shared_task

from .mail import deliver, report_failure

MAIL_RETRIES = 3


@shared_task(ignore_result=False)
def ping(word="pong"):
    """Проверка живости связки «сайт → Redis → воркер»: manage.py celery_check.

    ignore_result=False — единственная задача, ответ которой нам действительно нужен.
    """
    return word


# smtplib.SMTPException — наследник OSError, так что сюда попадают и обрыв сети,
# и отказ сервера. Пауза между попытками растёт, чтобы не долбить лежащий gmail.
@shared_task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_kwargs={"max_retries": MAIL_RETRIES})
def send_email(self, payload):
    """Письмо, собранное в веб-процессе (core.mail.pack). Отправляет воркер — своим
    соединением с SMTP, и он же повторяет попытку, если письмо не ушло."""
    try:
        deliver(payload)
    except Exception as error:
        # Повторяют только OSError и только пока попытки не вышли — всё прочее уже отказ.
        if not isinstance(error, OSError) or self.request.retries >= MAIL_RETRIES:
            report_failure(payload, error)
        raise
