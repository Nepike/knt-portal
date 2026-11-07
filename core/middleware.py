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
    """Строка в лог о запросе, который шёл дольше SLOW или поднял пик памяти на SWOLLEN.

    Полного журнала запросов в логе web нет: опрос агента печати каждые пару секунд
    заполнил бы его за сутки. А чтобы понять, что раздувает воркер или держит его минуты
    (3 ноя 2026 оба встали и были убиты по таймауту), хватает этих немногих строк.

    Пик — общий на процесс, а запросы в воркере идут параллельно в потоках: рост мог
    принести и сосед. Но то, что раз за разом всплывает одним и тем же адресом, и есть ответ.
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
    """Две поправки на то, что по одному адресу у нас два разных ответа.

    `Vary: HX-Request` — иначе браузер, вернувшись «назад» со страницы материала,
    показывал вместо страницы голый список: фильтр отвечает по тому же адресу, что
    и сама страница (его в адрес кладёт HX-Push-Url), только куском разметки. Ответ
    оседает в кеше, и по «назад» браузер достаёт оттуда кусок и рисует его целым
    документом — без шапки, меню и самих фильтров. Заголовок разводит эти два ответа.

    Редирект в ответ на htmx-запрос (например, на логин после истечения сессии)
    вставил бы страницу целиком в кусок DOM. Превращаем его в HX-Redirect —
    htmx делает полный переход браузером.
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
    """Заслон: пока сайт закрыт, любой адрес отвечает страницей закрытия (core/lockdown.py).

    Стоит за аутентификацией: ему нужен уже узнанный человек. Отвечает сам, ещё до
    разбора адреса, — поэтому гость получает объяснение, а не форму входа:
    LoginRequiredMiddleware спрашивает вход позже, в process_view.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Сначала адрес, потом база: раздача файлов — самый горячий путь на сайте,
        # и платить запросом за каждый сегмент видео ей незачем.
        if lockdown.always_open(request.path_info):
            return self.get_response(request)
        lock = lockdown.current()
        # Для base.html: прошедшему по белому списку шапка напоминает, что сайт закрыт.
        request.lockdown = lock
        if lock is None or lockdown.lets_in(request.user):
            return self.get_response(request)
        return lockdown.refusal(request, lock)
