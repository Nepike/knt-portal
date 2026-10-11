from django.http import HttpResponse
from django.utils.cache import patch_vary_headers

from . import lockdown


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
