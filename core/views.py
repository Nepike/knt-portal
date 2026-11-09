from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.db import DatabaseError, connection
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from . import inquiries, lockdown
from .forms import DemoForm, FeedbackForm, SupportForm
from .models import Funding, Inquiry, Lockdown


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
@never_cache
@require_safe
def healthz(request):
    """Жив ли сайт. Деплой (tools/wait_healthy.sh) ждёт здесь хеш своего коммита: старый
    контейнер, оставшийся отвечать, тоже сказал бы «ok».

    Трогает базу — без неё сайт не работает. Redis не трогает: без него страницы
    открываются, ждут только чат и очередь, и валить из-за этого выкладку не за что.
    Хеш не секрет — код открыт.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return HttpResponse("база не отвечает\n", status=503, content_type="text/plain; charset=utf-8")
    version = settings.SITE_VERSION.partition(" ")[0]
    return HttpResponse(f"ok {version}".rstrip() + "\n", content_type="text/plain; charset=utf-8")


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
    form = FeedbackForm(request.POST or None, request.FILES or None, known=request.user.is_authenticated)
    if request.method == "POST" and form.is_valid():
        # Открыта всему интернету и толкает сообщения в чат — гостя проверяют
        # штрафные баллы и ограничитель (core/inquiries.py).
        inquiries.take(request, Inquiry.Source.CONTACTS, form)
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
    form = SupportForm(request.POST or None, request.FILES or None, known=request.user.is_authenticated)
    if request.method == "POST" and form.is_valid():
        # Форма открыта всему интернету и толкает сообщения в чат — без ограничителя и
        # фильтра спама его завалило бы за вечер (core/inquiries.py).
        inquiries.take(request, Inquiry.Source.SUPPORT, form)
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
