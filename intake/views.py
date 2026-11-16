"""Ручки приёмки для пекарни (`tools/bake`): `@login_not_required` и токен `INTAKE_TOKEN`,
ответы — JSON с текстом причины.
"""

import posixpath
from pathlib import PurePosixPath
from uuid import uuid4

from django.contrib.auth.decorators import login_not_required
from django.core import signing
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from attachments.storage import content_type
from attachments.uploads import sign_download, sign_put, under
from core import agents
from notifications import services as notifications

from .health import heard
from .models import MediaJob, failed_bake, sweep, take
from .spec import MASTER, POSTER, RECIPES, check_ladder, payload
from .tasks import drop_source


def guard(request):
    """Ответ-отказ или None, если пускаем. Общий для всех ручек приёмки (core/agents.py)."""
    return agents.guard(request, "INTAKE_TOKEN")


@login_not_required
@require_GET
def spec(request):
    """Текущая спека: пекарня читает рецепт у сайта — её копия может отстать."""
    return guard(request) or JsonResponse(payload())


# ── Очередь ───────────────────────────────────────────────────────────────────
#
# Пекарня приходит сама: `claim` → скачать сырьё → испечь → `plan` → `sign` → залить
# напрямую в R2 → `commit`. Ключей от бакета у неё нет, только подписанные ссылки.
# Два выхода на полпути: `fail` — не вышло, `release` — остановили руками.

JOB_SALT = "intake.job"
# Столько живёт токен задания — заведомо дольше выпечки.
JOB_MAX_AGE = 12 * 3600
# Ссылок за заход: у двухчасовой лекции кусков 2400.
MAX_SIGNED = 200


def _job(payload):
    """Задание по токену или None. В токене и НОМЕР ПОПЫТКИ: медленной пекарне, у которой
    задание уже отдали другой, старый токен не подойдёт (попытку увеличивает `take`)."""
    try:
        number, attempt = signing.loads(payload.get("token", ""), salt=JOB_SALT, max_age=JOB_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        return None
    return MediaJob.objects.filter(
        pk=number, attempts=attempt, status=MediaJob.Status.BAKING,
    ).first()


def _gone():
    return JsonResponse({"error": "задание закрыто или отдано другому"}, status=409)


def _still_here(job):
    """Всякое слово пекарни продлевает срок: CLAIM_TIMEOUT — от ПОСЛЕДНЕЙ вести, иначе
    работающую машину лишили бы задания посреди скачивания или выпечки."""
    job.claimed_at = timezone.now()
    job.save(update_fields=["claimed_at", "updated"])


@login_not_required
@csrf_exempt
@require_POST
def claim(request):
    """Взять следующее задание. Пусто — значит спи дальше."""
    if refusal := guard(request):
        return refusal

    body = agents.body(request)
    worker = str(body.get("worker") or "пекарня")
    # С бедой (tools/bake.checkup) пекарня работу не берёт: отказ пришлось бы снимать руками.
    problem = str(body.get("problem") or "")
    heard(worker, problem)
    if problem:
        return JsonResponse({"job": None})
    job = take(worker)
    if job is None:
        return JsonResponse({"job": None})
    return JsonResponse({"job": {
        "id": job.pk,
        "recipe": job.recipe,
        "token": signing.dumps([job.pk, job.attempts], salt=JOB_SALT),
        "source": sign_download(job.source),
        "name": PurePosixPath(job.source).name,
    }})


@login_not_required
@csrf_exempt
@require_POST
def plan(request):
    """Манифест готового → сверка со спекой → папка для заливки. Сверяем ДО заливки кусков."""
    if refusal := guard(request):
        return refusal
    body = agents.body(request)
    job = _job(body)
    if job is None:
        return _gone()

    recipe = RECIPES.get(job.recipe)
    if recipe is None:
        return JsonResponse({"error": f"рецепта «{job.recipe}» больше нет"}, status=400)
    try:
        problem = check_ladder(recipe, body.get("manifest") or {})
    except (KeyError, TypeError, ValueError):
        problem = "манифест не разобрать"
    if problem:
        return JsonResponse({"error": problem}, status=400)

    # Папка всегда новая: поверх недолитых кусков вышел бы набор из двух выпечек. Прежнюю —
    # через `sweep`: у задания после успеха это набор живой лекции.
    stale = job.prefix
    job.prefix = f"lectures/{uuid4().hex}"
    job.manifest = body["manifest"]
    job.claimed_at = timezone.now()  # см. _still_here: срок идёт от последней вести
    job.save(update_fields=["prefix", "manifest", "claimed_at", "updated"])
    sweep(stale)
    return JsonResponse({"prefix": job.prefix})


@login_not_required
@csrf_exempt
@require_POST
def sign(request):
    """Подписанные ссылки на запись очередной порции кусков."""
    if refusal := guard(request):
        return refusal
    body = agents.body(request)
    job = _job(body)
    if job is None or not job.prefix:
        return _gone()

    _still_here(job)  # каждая порция заливки — весточка
    urls, types = {}, {}
    for name in list(body.get("names") or [])[:MAX_SIGNED]:
        # Ключ подписываем мы: «../» в имени увело бы запись в чужую папку.
        clean = posixpath.normpath(str(name)).lstrip("/")
        if ".." in clean.split("/"):
            return JsonResponse({"error": f"нехорошее имя: {name}"}, status=400)
        urls[name] = sign_put(f"{job.prefix}/{clean}")
        # Тип объявляем МЫ: он ложится в объект, список один на оба конца (storage.content_type).
        types[name] = content_type(clean)
    return JsonResponse({"urls": urls, "types": types})


def _incomplete(job):
    """Чего не хватает в залитом наборе, или None — считаем ключи одним листингом."""
    found = under(job.prefix)
    manifest = job.manifest
    # Мастер-манифест и обложка, плюс у каждой дорожки её манифест и init-кусок.
    expected = 2 + sum(one["segments"] + 2 for one in manifest.get("renditions", []))
    for name in (manifest.get("master", MASTER), POSTER):
        if f"{job.prefix}/{name}" not in found:
            return f"в хранилище нет {name}"
    if len(found) < expected:
        return f"залито {len(found)} кусков из {expected} — заливка оборвалась"
    return None


@login_not_required
@csrf_exempt
@require_POST
def commit(request):
    """«Залил». Сайт проверяет, что всё обещанное на месте, и привязывает к лекции."""
    if refusal := guard(request):
        return refusal
    job = _job(agents.body(request))
    if job is None or not job.prefix:
        return _gone()
    if problem := _incomplete(job):
        return JsonResponse({"error": problem}, status=400)

    # Прошлый набор этой лекции (перепечка) — после переезда не нужен.
    replaced = ""
    with transaction.atomic():
        if job.lecture_id:
            replaced = job.lecture.prefix
            job.lecture.prefix = job.prefix
            job.lecture.duration = int(job.manifest.get("duration") or 0)
            job.lecture.save(update_fields=["prefix", "duration"])
        job.status = MediaJob.Status.DONE
        job.note = ""
        job.save(update_fields=["status", "note", "updated"])
    notifications.baked(job)
    # Сырьё больше не нужно.
    source = job.source
    transaction.on_commit(lambda: drop_source.delay(source))
    # Через `sweep`: «прошлая» папка при повторном `commit` — та, что только что поставлена.
    sweep(replaced)
    return JsonResponse({"ok": True})


@login_not_required
@csrf_exempt
@require_POST
def alive(request):
    """«Ещё работаю»: скачивание и выпечка идут без запросов к сайту. Стучит сама работа
    (`tools/bake.Beat`), а не таймер: таймер держал бы задание и за повисшей машиной."""
    if refusal := guard(request):
        return refusal
    job = _job(agents.body(request))
    if job is None:
        return _gone()
    _still_here(job)
    heard(job.claimed_by)  # за долгим заданием пекарня работу не спрашивает, но и не молчит
    return JsonResponse({"ok": True})


@login_not_required
@csrf_exempt
@require_POST
def release(request):
    """«Не доделаю» (остановили руками) — задание сразу обратно в очередь. Не `fail`: печь
    можно, а отказ вынимают руками. Недолитую папку снимаем. Токен умирает сам: `_job` ищет
    задание «печётся»."""
    if refusal := guard(request):
        return refusal
    job = _job(agents.body(request))
    if job is None:
        return _gone()

    stale, job.prefix = job.prefix, ""
    job.status = MediaJob.Status.WAITING
    job.claimed_by = ""
    job.claimed_at = None
    job.save(update_fields=["status", "prefix", "claimed_by", "claimed_at", "updated"])
    sweep(stale)
    return JsonResponse({"ok": True})


@login_not_required
@csrf_exempt
@require_POST
def fail(request):
    """Не вышло. Причину показываем человеку — иначе лекция висит «печётся» вечно."""
    if refusal := guard(request):
        return refusal
    body = agents.body(request)
    job = _job(body)
    if job is None:
        return _gone()

    # Недолитое снимаем сразу: повтора может не быть, а искать папку потом не по чему.
    stale, job.prefix = job.prefix, ""
    job.status = MediaJob.Status.FAILED
    job.note = str(body.get("error", ""))[:300]
    job.save(update_fields=["status", "note", "prefix", "updated"])
    sweep(stale)
    failed_bake(job)
    return JsonResponse({"ok": True})
