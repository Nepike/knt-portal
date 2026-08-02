from django.http import HttpResponse


class HtmxRedirectMiddleware:
    """Редирект в ответ на htmx-запрос (например, на логин после истечения сессии)
    вставил бы страницу целиком в кусок DOM. Превращаем его в HX-Redirect —
    htmx делает полный переход браузером."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.headers.get("HX-Request") and 300 <= response.status_code < 400:
            return HttpResponse(headers={"HX-Redirect": response["Location"]})
        return response
