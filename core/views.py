from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.db import DatabaseError, connection
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from . import inquiries, lockdown
from .context_processors import today as site_today
from .forms import DemoForm, FeedbackForm, SupportForm
from .models import Funding, Inquiry, Lockdown


@login_not_required
def home(request):
    """Корень: вошедшим — материалы, гостям — «О сайте». В глитч-скин — страница отсчёта
    (quest), в праздник — праздничная."""
    theme = site_today()[0]
    if theme == "glitch":
        return redirect("countdown")
    if theme == "party":
        return redirect("party")
    if not request.user.is_authenticated:
        return redirect("about")
    return redirect("material_list")


@login_not_required
@never_cache
@require_safe
def healthz(request):
    """Жив ли сайт: деплой (tools/wait_healthy.sh) ждёт здесь хеш своего коммита. Трогает
    базу, но не Redis: без него страницы открываются. Хеш не секрет — код открыт."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return HttpResponse("база не отвечает\n", status=503, content_type="text/plain; charset=utf-8")
    version = settings.SITE_VERSION.partition(" ")[0]
    return HttpResponse(f"ok {version}".rstrip() + "\n", content_type="text/plain; charset=utf-8")


@login_not_required
def about(request):
    """«О сайте»: месяцы сбора и чек со спонсорами (core.Funding). Открыта всему интернету."""
    funding = Funding.shown()
    return render(request, "core/about.html", {"funding": funding, "names": funding.names() if funding else []})


@login_not_required
def contacts(request):
    """«Контакты», открыты всем. Форма как у поддержки, но в свой чат; телефоны деканата
    и студсовета — выше формы."""
    form = FeedbackForm(request.POST or None, request.FILES or None, known=request.user.is_authenticated)
    if request.method == "POST" and form.is_valid():
        # Гостя проверяют штрафные баллы и ограничитель (core/inquiries.py).
        inquiries.take(request, Inquiry.Source.CONTACTS, form)
        messages.success(request, "Спасибо, вопрос ушёл. Ответим на указанный контакт.")
        return redirect("contacts")

    return render(request, "core/contacts.html", {"form": form})


@login_not_required
def support(request):
    """Открыта и без входа: чаще всего пишут те, кто не может войти."""
    form = SupportForm(request.POST or None, request.FILES or None, known=request.user.is_authenticated)
    if request.method == "POST" and form.is_valid():
        inquiries.take(request, Inquiry.Source.SUPPORT, form)
        messages.success(request, "Спасибо, сообщение ушло. Разберёмся.")
        return redirect("support")

    return render(request, "core/support.html", {"form": form})


def closed(request):
    """Страница закрытия глазами остальных. Пока сайт открыт — черновик для того, кто
    правит запись; прочим 404."""
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
