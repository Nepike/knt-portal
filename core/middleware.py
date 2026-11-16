import logging
import time

from django.http import HttpResponse
from django.utils.cache import patch_vary_headers

from . import lockdown

try:
    import resource
except ImportError:  # windows: модуля нет, пиковую память там не узнать
    resource = None

slow_log = logging.getLogger("knt.slow")

SLOW = 2.0  # секунд на запрос
SWOLLEN = 20 * 1024  # КБ прироста пиковой памяти процесса


def peak_kb():
    """Пиковая память процесса за всю его жизнь, КБ (Linux), или None."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None


class SlowRequestMiddleware:
    """Строка в лог о запросе дольше SLOW или с ростом пика памяти больше SWOLLEN.

    Полного журнала запросов нет: опрос агента печати забил бы его за сутки. Пик общий
    на процесс, рост мог принести соседний поток — смотреть на повторяющиеся адреса.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started, before = time.monotonic(), peak_kb()
        response = self.get_response(request)
        spent = time.monotonic() - started
        grew = peak_kb() - before if before is not None else 0
        if spent >= SLOW or grew >= SWOLLEN:
            user = getattr(getattr(request, "user", None), "pk", None)
            slow_log.warning(
                "%s %s → %s за %.1f с, пик памяти +%d МБ, человек %s",
                request.method, request.path, response.status_code, spent, grew // 1024, user or "-",
            )
        return response


class HtmxMiddleware:
    """По одному адресу у нас два ответа: страница и кусок для htmx.

    `Vary: HX-Request` — иначе по «назад» браузер достаёт из кеша кусок и рисует его
    целой страницей. Редирект на htmx-запрос становится HX-Redirect: иначе страница
    целиком встала бы в кусок DOM.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        patch_vary_headers(response, ("HX-Request",))
        if request.headers.get("HX-Request") and 300 <= response.status_code < 400:
            return HttpResponse(headers={"HX-Redirect": response["Location"], "Vary": "HX-Request"})
        return response


class LockdownMiddleware:
    """Пока сайт закрыт, любой адрес отвечает страницей закрытия (core/lockdown.py).
    Стоит за аутентификацией и отвечает до LoginRequiredMiddleware: гость видит
    объяснение, а не форму входа."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Сначала адрес, потом база: раздаче файлов запрос на каждый сегмент видео ни к чему.
        if lockdown.always_open(request.path_info):
            return self.get_response(request)
        lock = lockdown.current()
        # Для base.html: прошедшему по белому списку шапка напоминает, что сайт закрыт.
        request.lockdown = lock
        if lock is None or lockdown.lets_in(request.user):
            return self.get_response(request)
        return lockdown.refusal(request, lock)
