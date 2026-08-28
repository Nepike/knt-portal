"""Ручки приёмки. Ходит сюда пекарня (`tools/bake`), а не человек.

Отсюда две особенности. Логин не годится: у пекарни нет ни браузера, ни сессии, —
поэтому `@login_not_required` и токен из `INTAKE_TOKEN`. И ответы — JSON с текстом
причины: на той стороне скрипт, который должен напечатать внятное, а не «401».
"""

import json
import posixpath
import secrets
from pathlib import PurePosixPath
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.core import signing
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from attachments.uploads import sign_download, sign_put, under

from .models import MediaJob, sweep, take
from .spec import MASTER, POSTER, RECIPES, check_ladder, payload
from .tasks import drop_source

BEARER = "Bearer "


def unusable(token):
    """Почему таким токеном пользоваться нельзя, или None.

    Заголовки HTTP переносят только латиницу: кириллический токен клиент физически
    не отправит, а пробел разорвёт заголовок. Отвергаем это КАК ненастроенность —
    иначе токен «работал бы» в тестах, а пекарня падала бы на кодировке.

    Пробел ловится не для красоты: django-environ не срезает хвост после решётки,
    и `INTAKE_TOKEN=abc # заметка` даёт значение вместе с заметкой (на это в проекте
    уже наступали). Такой токен лучше назвать сломанным сразу.
    """
    if not token:
        return "приёмка не настроена: пустой INTAKE_TOKEN"
    if not token.isascii() or not token.isprintable() or " " in token:
        return "INTAKE_TOKEN годится только из латиницы, цифр и знаков, без пробелов: заголовки HTTP другого не несут"
    return None


def _authorised(request):
    header = request.headers.get("Authorization", "")
    if not header.startswith(BEARER):
        return False
    # compare_digest, а не ==: сравнение строк выходит на первом несовпавшем символе,
    # и по времени ответа токен подбирается посимвольно.
    #
    # Байты, а не строки: строковый вариант отказывается сравнивать что угодно за
    # пределами латиницы, а ЗАГОЛОВОК — это чужой ввод. Настройка своя и проверена
    # в unusable(), но присланное проверить нельзя, и падать на нём вьюха не должна.
    return secrets.compare_digest(header[len(BEARER):].encode(), settings.INTAKE_TOKEN.encode())


def guard(request):
    """Ответ-отказ или None, если пускаем. Общий для всех ручек приёмки."""
    # Ненастроенность — это НЕ «пускаем всех»: ручки открыты в интернет без логина,
    # и забытая или битая настройка не должна превращаться в открытую дверь.
    if problem := unusable(settings.INTAKE_TOKEN):
        return JsonResponse({"error": problem}, status=503)
    if not _authorised(request):
        return JsonResponse({"error": "нужен заголовок Authorization: Bearer <INTAKE_TOKEN>"}, status=401)
    return None


@login_not_required
@require_GET
def spec(request):
    """Текущая спека. Пекарня читает рецепт у сайта, а не помнит свой: её копия
    репозитория может быть недельной давности, а требования — сегодняшние."""
    return guard(request) or JsonResponse(payload())


# ── Очередь ───────────────────────────────────────────────────────────────────
#
# Пекарня приходит сама: `claim` → скачать сырьё → испечь → `plan` → `sign` → залить
# напрямую в R2 → `commit`. Ключей от бакета у неё нет, только подписанные ссылки.

JOB_SALT = "intake.job"
# Столько живёт выданный токен задания — заведомо больше, чем печётся лекция.
JOB_MAX_AGE = 12 * 3600
# Ссылок за один заход. У двухчасовой лекции кусков 2400, и все разом — полтора
# мегабайта ответа и лишний риск, что они протухнут по дороге.
MAX_SIGNED = 200


def _body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


def _job(payload):
    """Задание по присланному токену или None. Номер приходит от пекарни, и без подписи
    она могла бы закрыть чужое.

    В токене не только номер, но и НОМЕР ПОПЫТКИ. Пекарня, промолчавшая дольше
    `CLAIM_TIMEOUT`, не умерла — она просто медленная, и задание к тому времени уже отдано
    другой машине. По одному номеру задания обе оставались бы полноправными: первая
    доделала бы своё и снесла папку второй. Попытку увеличивает `take`, поэтому старый
    токен перестаёт подходить ровно в тот миг, когда работу передали.
    """
    try:
        number, attempt = signing.loads(payload.get("token", ""), salt=JOB_SALT, max_age=JOB_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        return None
    return MediaJob.objects.filter(
        pk=number, attempts=attempt, status=MediaJob.Status.BAKING,
    ).first()


def _gone():
    return JsonResponse({"error": "задание закрыто или отдано другому"}, status=409)


@login_not_required
@csrf_exempt
@require_POST
def claim(request):
    """Взять следующее задание. Пусто — значит спи дальше."""
    if refusal := guard(request):
        return refusal

    job = take(str(_body(request).get("worker", "пекарня")))
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
    """Манифест готового → сверка со спекой → папка, куда его класть.

    Сверяем ДО заливки: отвергнуть описание дешевле, чем принять две тысячи кусков
    и обнаружить, что дорожка не та.
    """
    if refusal := guard(request):
        return refusal
    body = _body(request)
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

    # Папка всегда новая, даже на повторе: класть свежие куски поверх недолитых —
    # значит получить набор из двух выпечек, где половина сегментов от прошлой.
    # А недолитое надо СНЯТЬ, иначе каждая упавшая на заливке пекарня оставляла бы
    # в бакете по гигабайту, которого потом ничем не найти. Через `sweep`, потому что
    # прошлая папка бывает и не недолитой: у задания, возвращённого в очередь после
    # успеха, это набор живой лекции (см. sweep).
    stale = job.prefix
    job.prefix = f"lectures/{uuid4().hex}"
    job.manifest = body["manifest"]
    job.save(update_fields=["prefix", "manifest", "updated"])
    sweep(stale)
    return JsonResponse({"prefix": job.prefix})


@login_not_required
@csrf_exempt
@require_POST
def sign(request):
    """Подписанные ссылки на запись очередной порции кусков."""
    if refusal := guard(request):
        return refusal
    body = _body(request)
    job = _job(body)
    if job is None or not job.prefix:
        return _gone()

    urls = {}
    for name in list(body.get("names") or [])[:MAX_SIGNED]:
        # Имя даёт пекарня, а ключ подписываем мы: «../» увело бы запись в чужую папку.
        clean = posixpath.normpath(str(name)).lstrip("/")
        if ".." in clean.split("/"):
            return JsonResponse({"error": f"нехорошее имя: {name}"}, status=400)
        urls[name] = sign_put(f"{job.prefix}/{clean}")
    return JsonResponse({"urls": urls})


def _incomplete(job):
    """Чего не хватает в залитом наборе, или None.

    Считаем ключи, а не спрашиваем про каждый: их тысячи. Без этой проверки оборванная
    заливка прошла бы незамеченной, и лекция открылась бы наполовину.
    """
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
    job = _job(_body(request))
    if job is None or not job.prefix:
        return _gone()
    if problem := _incomplete(job):
        return JsonResponse({"error": problem}, status=400)

    # Прошлый набор этой же лекции — задание могли вернуть в очередь и перепечь.
    # Тогда лекция переезжает на новую папку, а старая остаётся никому не нужной.
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
    # Сырьё больше не нужно: гигабайты, из которых уже всё взяли.
    source = job.source
    transaction.on_commit(lambda: drop_source.delay(source))
    # Через `sweep`, а не напрямую: он же и убережёт от повторного `commit`, когда
    # «прошлая» папка — это та самая, что мы только что поставили лекции.
    sweep(replaced)
    return JsonResponse({"ok": True})


@login_not_required
@csrf_exempt
@require_POST
def fail(request):
    """Не вышло. Причину показываем человеку — иначе лекция висит «печётся» вечно."""
    if refusal := guard(request):
        return refusal
    body = _body(request)
    job = _job(body)
    if job is None:
        return _gone()

    # Недолитое снимаем прямо здесь, а не откладываем до следующей попытки: попытки
    # может и не быть. Самый обычный отказ — оборвавшаяся ЗАЛИВКА, то есть папка уже
    # с гигабайтом кусков; брошенная, она осталась бы в бакете навсегда, потому что
    # искать её потом не по чему. Заодно чистим и поле — оно указывало бы в пустоту.
    stale, job.prefix = job.prefix, ""
    job.status = MediaJob.Status.FAILED
    job.note = str(body.get("error", ""))[:300]
    job.save(update_fields=["status", "note", "prefix", "updated"])
    sweep(stale)
    return JsonResponse({"ok": True})
