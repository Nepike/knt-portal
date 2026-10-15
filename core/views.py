from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from telegram.notify import FEEDBACK, SUPPORT, notify

from . import lockdown
from .forms import DemoForm, FeedbackForm, SupportForm
from .models import Funding, Lockdown
from .throttle import client_ip, throttled


@login_not_required
def home(request):
    """Корень домена: своим — в материалы, чужим — на страницу «О сайте».

    Без `login_not_required` корень уводил бы незалогиненного на форму входа, а нам
    нужна витрина: на knt-mipt.ru приходят и те, кто сюда только собирается поступать.
    Своей главной по-прежнему нет; материалы — самый нужный раздел для вошедших.
    """
    if not request.user.is_authenticated:
        return redirect("about")
    return redirect("material_list")


@login_not_required
def about(request):
    """«О сайте» — единственная страница, открытая всему интернету целиком.

    До 15 окт 2026 по этому адресу была страница для абитуриентов. Факультет отказался
    оплачивать сайт, её сняли (шаблон core/applicants.html оставлен), и теперь здесь —
    кто его разрабатывает и оплачивает: месяцы сбора и чек со спонсорами (core.Funding).
    """
    funding = Funding.shown()
    return render(request, "core/about.html", {"funding": funding, "names": funding.names() if funding else []})


@login_not_required
def contacts(request):
    """«Контакты» — вторая страница, открытая всему интернету.

    Форма — сестра поддержки (та же очередь, тот же ограничитель), но уезжает в свой чат:
    сюда пишут про поступление и общежитие, а не про сломанную страницу. Отвечают на это
    деканат и студсовет, поэтому их телефоны стоят ВЫШЕ формы: написать в чат и ждать —
    не единственный способ, и не всегда самый быстрый.
    """
    known = request.user.is_authenticated
    form = FeedbackForm(request.POST or None, request.FILES or None, known=known)
    if request.method == "POST" and form.is_valid():
        # Открыта всему интернету и толкает сообщения в чат — тем же порядком, что и
        # поддержка. Молчим о лимите: спамить в ответ подсказками незачем.
        if not throttled(f"feedback:ip:{client_ip(request)}", 10):
            notify(FEEDBACK, "telegram/feedback.html", {
                **form.cleaned_data,
                "author": request.user if known else None,
                "profile_url": request.build_absolute_uri(reverse("profile", args=[request.user.pk])) if known else "",
            }, image=form.cleaned_data.get("image"))
        messages.success(request, "Спасибо, вопрос ушёл. Ответим на указанный контакт.")
        return redirect("contacts")

    return render(request, "core/contacts.html", {"form": form})


@login_not_required
def support(request):
    """Открыта и без входа: чаще всего пишут как раз те, кто не может войти.

    Адрес страницы, с которой пришли, раньше подставлялся из referer и уезжал в чат
    отдельной строкой — и сбивал с толку: предложение «верните подписи» приходило
    со ссылкой на случайный материал, будто речь про него.
    """
    author = request.user if request.user.is_authenticated else None
    form = SupportForm(request.POST or None, request.FILES or None, known=bool(author))
    if request.method == "POST" and form.is_valid():
        # Форма открыта всему интернету и толкает сообщения в чат — без ограничителя
        # его завалило бы за вечер. Молчим о лимите: спамить в ответ подсказками незачем,
        # а живой человек столько обращений подряд всё равно не напишет.
        if throttled(f"support:ip:{client_ip(request)}", 10):
            messages.success(request, "Спасибо, сообщение ушло. Разберёмся.")
            return redirect("support")

        notify(SUPPORT, "telegram/support.html", {
            **form.cleaned_data,
            "author": author,
            # Кто человек и как с ним связаться — на его странице; почту в чат не тащим.
            "profile_url": request.build_absolute_uri(reverse("profile", args=[author.pk])) if author else "",
            # Поверх cleaned_data, а не до: там лежит код («broken»), а в чат нужно словами.
            "topic": dict(SupportForm.TOPICS)[form.cleaned_data["topic"]],
        }, image=form.cleaned_data.get("image"))
        messages.success(request, "Спасибо, сообщение ушло. Разберёмся.")
        return redirect("support")

    return render(request, "core/support.html", {"form": form})


def closed(request):
    """Страница закрытия, какой её видят остальные.

    Пока сайт открыт, это черновик, и видит его только тот, кто правит запись, —
    вычитать текст до включения. Остальным 404, а не отказ: объявления, которого
    ещё нет, для них нет вовсе. Когда сайт закрыт, сюда ведёт напоминание в шапке
    у белого списка, а всем прочим ту же страницу показывает заслон, по любому адресу.
    """
    lock = Lockdown.load()
    if not request.user.has_perm(lockdown.PASS if lock.active else lockdown.EDIT):
        raise Http404
    return lockdown.page(request, lock)


def demo(request):
    form = DemoForm(request.GET or None)
    submitted = form.cleaned_data if form.is_valid() else None
    if submitted is not None:
        messages.success(request, "Форма прошла валидацию")
    elif form.is_bound:
        messages.error(request, "В форме есть ошибки")
    return render(request, "core/demo.html", {"form": form, "submitted": submitted})
