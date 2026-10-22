"""Ручки агента печати (`tools/print_agent.py` на М4). Человек сюда не ходит.

Агент приходит сам. Разбор: `claim` → скачать исходник → PDF, заливка, превью →
`analyzed` (цифры, в ответ — куда залить) → залить → `commit`. Печать: `claim` → скачать
print.pdf → отдать в CUPS → `sent` → дождаться CUPS → `printed`. Выходы на полпути: `fail`
(не вышло, с причиной для человека) и `release` (агента остановили — задание ещё можно
сделать). Ключей от бакета у агента нет, только подписанные ссылки. Договор — docs/printing.md.
"""

from functools import wraps
from pathlib import PurePosixPath

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.core import signing
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from attachments.storage import content_type
from attachments.uploads import sign_download, sign_put, under
from core import agents

from . import alerts, queue
from .models import Printer, PrinterReading, PrintJob

Status = PrintJob.Status
JOB_SALT = "printing.agent"
# Токен задания живёт неделю: печать без бумаги честно ждёт, хоть до утра, и агент
# должен суметь доложить «напечатано», когда бумагу положат. Старые попытки отсекает
# номер попытки в токене, а не срок.
JOB_MAX_AGE = 7 * 24 * 3600
ANALYZE, PRINT = "analyze", "print"
# Чем было задание у агента — в этом состоянии его ждут ручки.
WORKING = {ANALYZE: Status.ANALYZING, PRINT: Status.PRINTING}


def door(view):
    """Ручка для агента: без логина и CSRF, но только с PRINT_AGENT_TOKEN."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        return agents.guard(request, "PRINT_AGENT_TOKEN") or view(request, *args, **kwargs)
    return login_not_required(csrf_exempt(wrapped))


def _token(job, kind):
    return signing.dumps([job.uuid.hex, job.attempts, kind], salt=JOB_SALT)


def _job(payload):
    """(задание, вид работы) по токену или (None, None).

    В токене номер попытки: агент, промолчавший дольше срока, не умер, а просто медленный,
    и задание к тому времени выдано снова. По одному uuid обе попытки были бы полноправны.
    """
    try:
        uuid, attempt, kind = signing.loads(str(payload.get("token", "")), salt=JOB_SALT, max_age=JOB_MAX_AGE)
        job = PrintJob.objects.select_related("printer").filter(
            uuid=uuid, attempts=attempt, status=WORKING[kind],
        ).first()
    except (signing.BadSignature, TypeError, ValueError, KeyError):
        return None, None
    return (job, kind) if job else (None, None)


def _gone():
    return JsonResponse({"error": "задание закрыто или отдано другому"}, status=409)


def _bad(problem):
    return JsonResponse({"error": problem}, status=400)


def _heard(job, *fields):
    job.heard_at = timezone.now()
    job.save(update_fields=["heard_at", *fields])


@door
@require_GET
def config(request):
    """Принтеры и пределы. Агент читает их у сайта, а не хранит у себя: его копия
    репозитория бывает старше сегодняшних настроек."""
    return JsonResponse({
        "printers": [
            {"id": printer.pk, "name": printer.name, "queue": printer.queue, "address": printer.address,
             "active": printer.active, "duplex": printer.duplex}
            for printer in Printer.objects.all()
        ],
        "limits": {"max_file_mb": settings.PRINT_MAX_FILE_MB, "max_doc_pages": settings.PRINT_MAX_DOC_PAGES},
        "silence": {
            ANALYZE: int(queue.ANALYZE_SILENCE.total_seconds()),
            PRINT: int(queue.PRINT_SILENCE.total_seconds()),
            "sent": int(queue.SENT_SILENCE.total_seconds()),
        },
    })


@door
@require_POST
def claim(request):
    """Взять следующее задание: {"kind": "analyze" | "print"}. Пусто — {"job": null}."""
    kind = agents.body(request).get("kind")
    if kind not in WORKING:
        return _bad("kind — analyze или print")
    queue.settle()
    job = queue.take_analysis() if kind == ANALYZE else queue.take_print()
    if job is None:
        return JsonResponse({"job": None})

    answer = {"kind": kind, "uuid": job.uuid.hex, "token": _token(job, kind), "queue": job.printer.queue}
    if kind == ANALYZE:
        answer |= {
            "name": job.name, "extension": PurePosixPath(job.source_key).suffix.lower(),
            "source": sign_download(job.source_key),
        }
    else:
        answer |= {
            # По этому имени агент после перезапуска найдёт задание в CUPS (docs/printing.md,
            # «Защита от двойной печати»).
            "title": f"knt-{job.uuid.hex}", "pdf": sign_download(job.pdf_key), "pages": job.pages,
            "selection": job.selection, "copies": job.copies, "duplex": job.duplex, "sides": job.sides,
        }
    return JsonResponse({"job": answer})


def _shares(raw, pages):
    """Заливка страниц: ровно pages чисел от 0 до 1, или None."""
    if not isinstance(raw, list) or len(raw) != pages:
        return None
    try:
        shares = [round(float(share), 4) for share in raw]
    except (TypeError, ValueError):
        return None
    return shares if all(0 <= share <= 1 for share in shares) else None


@door
@require_POST
def analyzed(request):
    """Цифры разбора → проверка → куда залить print.pdf и превью.

    Ключи выбирает сайт, а не агент, — внутри папки задания. И тип объявляет сайт: он
    записывается в объект и потом уходит браузеру (attachments.storage.CONTENT_TYPES).
    """
    payload = agents.body(request)
    job, kind = _job(payload)
    if kind != ANALYZE:
        return _gone()

    pages = payload.get("pages")
    if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= settings.PRINT_MAX_DOC_PAGES:
        return _bad(f"pages — целое от 1 до {settings.PRINT_MAX_DOC_PAGES}")
    shares = _shares(payload.get("coverage"), pages)
    if shares is None:
        return _bad("coverage — по доле от 0 до 1 на каждую страницу")
    sideways = payload.get("sideways") or []
    if not isinstance(sideways, list) or not all(isinstance(n, int) and 1 <= n <= pages for n in sideways):
        return _bad("sideways — номера страниц документа")

    job.pages, job.coverage, job.sideways = pages, shares, sorted(set(sideways))
    _heard(job, "pages", "coverage", "sideways")
    names = ["print.pdf", *(f"pages/{number:04d}.jpg" for number in range(1, pages + 1))]
    # Крупные превью — отдельным полем, а не в urls: агент старше их не рисует, а по urls
    # заливает всё подряд и на незнакомом имени споткнулся бы.
    large = [f"large/{number:04d}.jpg" for number in range(1, pages + 1)]
    return JsonResponse({
        "urls": {name: sign_put(job.folder + name) for name in names},
        "large": {name: sign_put(job.folder + name) for name in large},
        "types": {name: content_type(name) for name in names + large},
    })


@door
@require_POST
def commit(request):
    """«Залил». Сайт сверяет ключи под папкой задания: оборванная заливка иначе открылась
    бы человеку сеткой с дырами, а печать — без print.pdf."""
    job, kind = _job(agents.body(request))
    if kind != ANALYZE:
        return _gone()
    if not job.pages:
        return _bad("сначала analyzed")
    expected = {job.folder + "print.pdf", *(f"{job.folder}pages/{number:04d}.jpg" for number in range(1, job.pages + 1))}
    found = under(job.folder.rstrip("/"))
    missing = expected - found
    if missing:
        return _bad(f"в хранилище нет {len(missing)} из {len(expected)}: {min(missing)}")

    job.status = Status.READY
    job.pdf_key = job.folder + "print.pdf"
    # Крупных может не быть (агент старше) — печати это не мешает, просмотрщик возьмёт мелкие.
    job.large = all(f"{job.folder}large/{number:04d}.jpg" in found for number in range(1, job.pages + 1))
    job.analyzed_at = timezone.now()
    job.reason = ""
    _heard(job, "status", "pdf_key", "large", "analyzed_at", "reason")
    return JsonResponse({"ok": True})


@door
@require_POST
def sent(request):
    """«Отдал в CUPS, номер такой-то». С этой минуты задание в очередь не вернётся никогда."""
    payload = agents.body(request)
    job, kind = _job(payload)
    if kind != PRINT:
        return _gone()
    number = payload.get("cups_job")
    if not isinstance(number, int) or isinstance(number, bool) or number < 0:
        return _bad("cups_job — номер задания CUPS")
    job.cups_job = number
    _heard(job, "cups_job")
    return JsonResponse({"ok": True})


@door
@require_POST
def printed(request):
    """CUPS сказал «выполнено». Это «принтер принял всё», а не «лист вышел» — сверку
    со счётчиком страниц делает агент до того, как сюда прийти.

    `{"sure": false}` — счётчик не сдвинулся: листов, похоже, нет. Это не «не вышло»
    (могли и выйти) и не «напечатано» — `unknown`, в лимит не идёт, решает человек у лотка.
    """
    payload = agents.body(request)
    job, kind = _job(payload)
    if kind != PRINT:
        return _gone()
    if payload.get("sure", True) is False:
        job.status = Status.UNKNOWN
        job.reason = "CUPS принял задание, но счётчик принтера не сдвинулся — проверь лоток"
    else:
        job.status = Status.DONE
    job.finished_at = timezone.now()
    _heard(job, "status", "reason", "finished_at")
    return JsonResponse({"ok": True})


@door
@require_POST
def fail(request):
    """Не вышло. Причина уходит человеку: разбор — «не принято», печать — «не вышло»,
    и страницы возвращаются в лимит сами (лимит считает только живое и напечатанное)."""
    payload = agents.body(request)
    job, kind = _job(payload)
    if job is None:
        return _gone()
    job.reason = str(payload.get("error") or "")[:300]
    if kind == ANALYZE:
        job.status = Status.REJECTED
        _heard(job, "status", "reason")
    else:
        job.status = Status.FAILED
        job.finished_at = timezone.now()
        _heard(job, "status", "reason", "finished_at")
    return JsonResponse({"ok": True})


@door
@require_POST
def alive(request):
    """«Ещё работаю» — и текст с панели принтера, чтобы студент видел «нет бумаги»."""
    payload = agents.body(request)
    job, kind = _job(payload)
    if job is None:
        return _gone()
    _heard(job)
    if kind == PRINT and (panel := payload.get("panel")) is not None:
        Printer.objects.filter(pk=job.printer_id).update(state_text=str(panel)[:200])
    return JsonResponse({"ok": True})


@door
@require_POST
def release(request):
    """«Не доделаю»: агента останавливают. Задание сразу снова в очереди, а не ждёт, пока
    сайт сочтёт агента пропавшим.

    Печать — только до `sent`. Отданное в CUPS могло уже выйти на бумагу, и вернуть его
    в очередь значит напечатать второй раз. Такое агент доводит сам после перезапуска.
    """
    job, kind = _job(agents.body(request))
    if job is None:
        return _gone()
    if kind == PRINT and job.cups_job is not None:
        return JsonResponse({"error": "задание уже у принтера — в очередь оно не вернётся"}, status=409)
    job.status = Status.UPLOADED if kind == ANALYZE else Status.QUEUED
    job.claimed_at = None
    job.save(update_fields=["status", "claimed_at"])
    return JsonResponse({"ok": True})


def _number(raw, top=None):
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0 or (top is not None and raw > top):
        return None
    return raw


@door
@require_POST
def status(request):
    """Показания принтеров по SNMP: [{id, state, panel, toner, counter}].

    Карточка принтера обновляется всегда — по времени показаний студент видит, жив ли
    агент. А в журнал показаний строка идёт, только если что-то поменялось: раз в минуту
    одно и то же за ночь — тысяча строк ни о чём.
    """
    now = timezone.now()
    known = {printer.pk: printer for printer in Printer.objects.all()}
    for item in agents.body(request).get("printers") or []:
        printer = known.get(item.get("id")) if isinstance(item, dict) else None
        if printer is None:
            continue
        state = item.get("state") if item.get("state") in Printer.State.values else Printer.State.UNKNOWN
        toner, counter = _number(item.get("toner"), 100), _number(item.get("counter"))
        if (state, toner, counter) != (printer.state, printer.toner_percent, printer.page_counter):
            PrinterReading.objects.create(printer=printer, at=now, state=state, toner_percent=toner, page_counter=counter)
        printer.state, printer.toner_percent, printer.page_counter = state, toner, counter
        printer.state_text = str(item.get("panel") or "")[:200]
        printer.seen_at = now
        printer.save(update_fields=["state", "state_text", "toner_percent", "page_counter", "seen_at"])
        alerts.check(printer)
    return JsonResponse({"ok": True})
