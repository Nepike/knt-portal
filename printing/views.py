"""Печать для студентов: загрузка, выбор страниц, очередь.

Документ сайт не открывает: превью и заливку приносит агент на М4, здесь — показ, выбор,
лимит и очередь. Раздел открыт всем вошедшим; кому печатать нельзя — галочка «печать
запрещена» в PrintAccount (админка): раздел он видит, загрузить и напечатать не может.
"""

import json
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from attachments.media import image_url
from attachments.storage import file_storage
from attachments.uploads import direct_upload, new_key, sign_upload, uploaded_file
from core.throttle import client_ip, throttled

from . import services
from .models import Printer, PrintJob
from .pricing import DARK, BadSelection, pages_of, selection_of

FOLDER = "printing"
# Ровно то, что умеет агент (tools/print_agent.SIGNATURES — их сверяет тест). Таблиц нет
# намеренно: XLSX печатается непредсказуемо, и проще попросить сохранить его в PDF.
ACCEPTED = (".pdf", ".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".odp", ".jpg", ".jpeg", ".png")
UPLOADS_PER_HOUR = 20
JOBS_SHOWN = 30


def max_size():
    return int(settings.PRINT_MAX_FILE_MB * 1024 * 1024)


def size_text():
    return f"{settings.PRINT_MAX_FILE_MB:g} МБ"


def refusal(name, size):
    """Почему такой файл не берём, или None. Содержимое проверит агент: здесь только имя и вес."""
    if Path(name).suffix.lower() not in ACCEPTED:
        return (f"«{name}» не напечатать. Подойдут PDF, DOC, DOCX, ODT, RTF, PPT, PPTX, ODP, JPG и PNG; "
                "таблицу сохрани в PDF")
    if size > max_size():
        return f"«{name}» больше {size_text()}. Сожми его или загрузи только нужную часть"
    if not size:
        return f"«{name}» пустой"
    return None


def _printer():
    """Под чей PPD разбирать загруженное. Все наши принтеры — A4, так что любой работающий;
    печатать человек потом выберет сам."""
    printers = list(Printer.objects.filter(active=True))
    return next((printer for printer in printers if printer.online), printers[0] if printers else None)


@require_GET
def home(request):
    context = {
        "banned": services.banned(request.user), "banned_text": services.BANNED,
        "printer": _printer(),
        "printers": Printer.objects.all(),
        "limits": services.limits(request.user),
        "jobs": request.user.print_jobs.select_related("printer")[:JOBS_SHOWN],
        "accept": ",".join(ACCEPTED),
        "max_size": size_text(),
        # Тот же компонент, что у материалов (fileForm в components.js), но своя подпись:
        # папка печати и свой потолок веса. Частями не льём — 50 МБ уходят одним PUT.
        "upload": json.dumps({
            "direct": direct_upload(), "maxSize": max_size(), "signUrl": reverse("print_sign"),
            "each": reverse("print_upload"), "maxFiles": 1, "single": True, "forbidden": [],
        }),
    }
    return render(request, "printing/home.html", context)


def _too_often(user):
    return throttled(f"print:upload:{user.pk}", UPLOADS_PER_HOUR)


@require_POST
def sign(request):
    """Подписанная ссылка: файл едет в хранилище браузером, мимо нашего процесса."""
    if not direct_upload():
        return JsonResponse({"error": "Прямая загрузка недоступна"}, status=409)
    try:
        payload = json.loads(request.body)
        name, size = str(payload["name"])[:150], int(payload["size"] or 0)
    except (ValueError, KeyError, TypeError):
        return HttpResponseBadRequest("Ожидались name и size")
    if services.banned(request.user):
        return JsonResponse({"error": services.BANNED}, status=403)
    if _printer() is None:
        return JsonResponse({"error": "Печать сейчас недоступна"}, status=409)
    if problem := refusal(name, size):
        return JsonResponse({"error": problem}, status=400)
    if _too_often(request.user):
        return JsonResponse({"error": "Слишком много загрузок за час — попробуй позже"}, status=429)
    url, token, kind = sign_upload(name, FOLDER)
    return JsonResponse({"url": url, "token": token, "type": kind})


@require_POST
def upload(request):
    """Файл уже в хранилище (или пришёл сюда, если хранилище — диск): заводим задание."""
    as_json = request.headers.get("Accept") == "application/json"

    def refuse(reason):
        if as_json:
            return JsonResponse({"error": reason}, status=400)
        messages.error(request, reason)
        return redirect("print_home")

    if services.banned(request.user):
        return refuse(services.BANNED)
    printer = _printer()
    if printer is None:
        return refuse("Печать сейчас недоступна")

    if token := request.POST.get("uploaded"):
        found = uploaded_file(token, FOLDER)
        if found is None:
            return refuse("Файл не доехал до хранилища — загрузи его заново")
        key, name = found
        # Задание по этому файлу уже есть: ответ на первую отправку потерялся, и форма
        # повторила запрос. Второго задания на тот же файл не заводим.
        if job := PrintJob.objects.filter(source_key=key, user=request.user).first():
            return _opened(job, as_json)
        try:
            size = file_storage().size(key)
        except FileNotFoundError:
            return refuse("Файл не доехал до хранилища — загрузи его заново")
        # Размер в подписи не участвует: положить по ссылке можно сколько угодно,
        # и настоящий вес виден только теперь.
        if problem := refusal(name, size):
            file_storage().delete(key)
            return refuse(problem)
    else:
        sent = request.FILES.get("file") or next(iter(request.FILES.getlist("files")), None)
        if sent is None:
            return refuse("Выбери файл")
        name = sent.name[:150]
        if problem := refusal(name, sent.size):
            return refuse(problem)
        if _too_often(request.user):
            return refuse("Слишком много загрузок за час — попробуй позже")
        key = file_storage().save(new_key(name, FOLDER), sent)

    job = PrintJob.objects.create(
        uuid=UUID(Path(key).parent.name), user=request.user, printer=printer, name=name, source_key=key,
    )
    return _opened(job, as_json)


def _opened(job, as_json):
    url = reverse("print_job", args=[job.uuid])
    return JsonResponse({"redirect": url}) if as_json else redirect(url)


def _own(request, uuid):
    return get_object_or_404(PrintJob.objects.select_related("printer"), uuid=uuid, user=request.user)


@require_GET
def job(request, uuid):
    job = _own(request, uuid)
    limits = services.limits(request.user)
    pictures = list(job.pictures()) if job.pages and not job.files_dropped else []
    try:
        chosen = set(pages_of(job.selection, job.pages))
    except BadSelection:
        chosen = set(range(1, job.pages + 1))
    for picture in pictures:
        picture["url"] = image_url(picture["key"])
        picture["zoom"] = image_url(picture["large"])
        picture["chosen"] = picture["number"] in chosen
    context = {
        "job": job, "limits": limits, "pictures": pictures, "dark": round(DARK * 100),
        # Просмотрщику (lightbox в components.js) — крупные страницы, если агент их нарисовал.
        "zoom": [{"url": picture["zoom"], "name": f"Страница {picture['number']}"} for picture in pictures],
        "outside": client_ip(request) not in settings.PRINT_M4_IPS,
        "max_copies": services.MAX_COPIES,
        "banned": services.banned(request.user), "banned_text": services.BANNED,
    }
    if job.can_print and not context["banned"]:
        printers = list(Printer.objects.all())
        ready = [printer for printer in printers if printer.available]
        chosen = job.printer if job.printer.available else next(iter(ready), None)
        context["printers"] = printers
        # Сумма на экране — прикидка для человека; в задание идёт расчёт сервера (pricing.py).
        context["pick"] = {
            "pages": job.pages, "coverage": job.coverage, "selection": job.selection or selection_of(range(1, job.pages + 1)),
            "copies": job.copies, "duplex": job.duplex, "left": limits["left"], "maxCopies": services.MAX_COPIES,
            "printer": chosen.pk if chosen else None,
            "printers": {printer.pk: {"duplex": printer.duplex, "costs": printer.costs().as_dict()} for printer in ready},
        }
    return render(request, "printing/job.html", context)


@require_GET
def status(request, uuid):
    """Опрос со страницы задания. Сменилось состояние — перерисовать страницу целиком:
    у каждого своя раскладка (превью, выбор, кнопки), и заплатки к ней были бы хрупче."""
    job = _own(request, uuid)
    if request.GET.get("was") != job.status:
        return HttpResponse(headers={"HX-Refresh": "true"})
    return render(request, "printing/_status.html", {"job": job})


@require_POST
def submit(request, uuid):
    job = _own(request, uuid)
    try:
        services.submit(
            job, request.POST.get("selection", ""), request.POST.get("copies", 1),
            request.POST.get("duplex") == "on", client_ip(request), request.POST.get("printer"),
        )
    except services.Refused as problem:
        messages.error(request, str(problem))
    return redirect("print_job", uuid=job.uuid)


@require_POST
def cancel(request, uuid):
    job = _own(request, uuid)
    try:
        services.cancel(job)
    except services.Refused as problem:
        messages.error(request, str(problem))
    return redirect("print_job", uuid=job.uuid)


@require_POST
def remove(request, uuid):
    job = _own(request, uuid)
    try:
        services.remove(job)
    except services.Refused as problem:
        messages.error(request, str(problem))
        return redirect("print_job", uuid=job.uuid)
    messages.success(request, f"Задание «{job.name}» удалено")
    return redirect("print_home")


@require_POST
def again(request, uuid):
    job = _own(request, uuid)
    if _too_often(request.user):
        messages.error(request, "Слишком много заданий за час — попробуй позже")
        return redirect("print_job", uuid=job.uuid)
    try:
        copy = services.again(job)
    except services.Refused as problem:
        messages.error(request, str(problem))
        return redirect("print_job", uuid=job.uuid)
    return redirect("print_job", uuid=copy.uuid)
