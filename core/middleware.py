from django.http import HttpResponse
from django.shortcuts import render
from django.utils.cache import patch_vary_headers

from .beta import locked


class BetaLockMiddleware:
    """Закрытые на время беты разделы (список — core/beta.py).

    Проверяем в process_view, а не в __call__: имя урла появляется только после того,
    как Django разобрал адрес, а до этого знать, куда человек идёт, неоткуда.

    Отвечаем страницей с объяснением и 403, а не редиректом на материалы: молча увести
    в другой раздел — значит оставить человека гадать, почему ссылка «не работает».
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if locked(request):
            return render(request, "core/locked.html", status=403)
        return None


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
