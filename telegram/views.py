"""Ящик для архивов аналитики со второго сервера. Человек сюда не ходит: ни форм, ни страниц.

Скрипт шлёт архив одной строкой:

    curl -fsS -H "Authorization: Bearer $ANALYTICS_TOKEN" -F file=@stats.tar.gz -F note="за неделю" \\
        https://knt-mipt.ru/inbox/analytics/

Сайт архив не хранит — ни в R2, ни на диске: байты уезжают через очередь в чат admin
(`tasks.send_document`). Ответ — JSON с причиной, как у пекарни и агента печати
(core/agents.py): на той стороне скрипт, ему нужно внятное, а не голый код.
"""

import logging
from base64 import b64encode

from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from attachments.models import human_size
from core import agents
from core.throttle import throttled
from knt.celery import app as celery_app

from .notify import ADMIN
from .tasks import send_document

logger = logging.getLogger(__name__)

# Байты едут в самой задаче через Redis (256 МБ, очередь не вытесняется) и раздуваются там
# на треть. Понадобится больше — передавать воркеру через общий диск ./media, а не поднимать предел.
MAX_ARCHIVE = 20 * 1024 * 1024
WRAPPING = 64 * 1024  # multipart-обвязка и заметка поверх самого файла
PER_HOUR = 30  # зациклившийся скрипт не должен завалить очередь и чат
NOTE_LIMIT = 500

# Архив узнаём по первым байтам, а не по имени: имя пишет скрипт. Внутрь не смотрим и не
# распаковываем — поэтому и zip-бомба здесь ничего не сделает.
SIGNATURES = (
    b"PK\x03\x04", b"PK\x05\x06",  # zip (второй — пустой)
    b"\x1f\x8b",                   # gzip, а с ним tar.gz
    b"BZh",                        # bzip2
    b"\xfd7zXZ\x00",               # xz
    b"7z\xbc\xaf\x27\x1c",         # 7z
    b"\x28\xb5\x2f\xfd",           # zstd
)
TAR_AT, TAR_MAGIC = 257, b"ustar"  # у tar подпись не в начале


def is_archive(head):
    return head.startswith(SIGNATURES) or head[TAR_AT:TAR_AT + len(TAR_MAGIC)] == TAR_MAGIC


def refuse(error, status):
    return JsonResponse({"error": error}, status=status)


def declared_size(request):
    try:
        return int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return 0


@login_not_required
@csrf_exempt
@require_POST
def analytics_inbox(request):
    if denied := agents.guard(request, "ANALYTICS_TOKEN"):
        return denied
    too_big = f"архив больше {human_size(MAX_ARCHIVE)}"
    # По заголовку и ДО разбора тела: лишнего незачем даже читать. Тело без длины
    # (chunked) поймает проверка размера самого файла ниже.
    if declared_size(request) > MAX_ARCHIVE + WRAPPING:
        return refuse(too_big, 413)
    # После токена, а не до: иначе чужие запросы выбирали бы лимит нашего скрипта.
    if throttled("analytics:inbox", PER_HOUR):
        return refuse(f"не больше {PER_HOUR} архивов в час", 429)

    upload = request.FILES.get("file")
    if upload is None:
        return refuse("нужен файл в поле file (multipart/form-data)", 400)
    if upload.size > MAX_ARCHIVE:
        return refuse(too_big, 413)
    if not is_archive(upload.read(512)):
        return refuse("это не архив: годятся zip, tar, tar.gz, 7z, xz, bz2, zst", 400)

    text = render_to_string("telegram/analytics.html", {
        "name": upload.name, "size": human_size(upload.size), "note": request.POST.get("note", "")[:NOTE_LIMIT],
    }).strip()
    upload.seek(0)
    try:
        # Соединение — одной попыткой и до отправки: сама отправка при лежащем Redis
        # переподключается до сотни раз, и скрипт висел бы минутами (так же в pulse/system.py).
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=0)
            send_document.apply_async(
                args=[ADMIN, text, b64encode(upload.read()).decode(), upload.name], connection=connection, retry=False,
            )
    except Exception:  # noqa: BLE001 — какое именно, зависит от транспорта; для скрипта это одно «повтори»
        logger.warning("Очередь недоступна, архив аналитики %s не принят", upload.name, exc_info=True)
        return refuse("очередь недоступна, повторите позже", 503)
    return JsonResponse({"ok": True, "name": upload.name, "size": upload.size})
