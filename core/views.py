from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.shortcuts import redirect, render
from django.urls import reverse

from telegram.notify import SUPPORT, notify

from .forms import DemoForm, SupportForm
from .throttle import client_ip, throttled


def home(request):
    """Корень домена. Своей главной пока нет — ведём в материалы, самый нужный раздел."""
    return redirect("material_list")


def _came_from(request):
    """Откуда пришли — чтобы не заставлять копировать адрес руками.

    Саму поддержку не подставляем: после отправки браузер шлёт её же, и следующее
    обращение уезжало бы с адресом страницы обращений.
    """
    referer = request.META.get("HTTP_REFERER", "")
    return "" if reverse("support") in referer else referer


@login_not_required
def support(request):
    """Открыта и без входа: чаще всего пишут как раз те, кто не может войти."""
    known = request.user.email if request.user.is_authenticated else ""
    form = SupportForm(request.POST or None, known=known, initial={"page": _came_from(request)})
    if request.method == "POST" and form.is_valid():
        # Форма открыта всему интернету и толкает сообщения в чат — без ограничителя
        # его завалило бы за вечер. Молчим о лимите: спамить в ответ подсказками незачем,
        # а живой человек столько обращений подряд всё равно не напишет.
        if throttled(f"support:ip:{client_ip(request)}", 10):
            messages.success(request, "Спасибо, сообщение ушло. Разберёмся.")
            return redirect("support")

        notify(SUPPORT, "telegram/support.html", {
            **form.cleaned_data,
            "author": request.user if request.user.is_authenticated else None,
            # Поверх cleaned_data, а не до: там лежит код («broken»), а в чат нужно словами.
            "topic": dict(SupportForm.TOPICS)[form.cleaned_data["topic"]],
        })
        messages.success(request, "Спасибо, сообщение ушло. Разберёмся.")
        return redirect("support")

    return render(request, "core/support.html", {"form": form})


def demo(request):
    form = DemoForm(request.GET or None)
    submitted = form.cleaned_data if form.is_valid() else None
    if submitted is not None:
        messages.success(request, "Форма прошла валидацию")
    elif form.is_bound:
        messages.error(request, "В форме есть ошибки")
    return render(request, "core/demo.html", {"form": form, "submitted": submitted})
