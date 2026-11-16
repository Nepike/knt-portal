"""Ночная уборка печати: файлы студентов живут не дольше суток, остаются только цифры
заданий. Папку знают ключи, а не uuid (повтор берёт файлы предшественника), поэтому
снимается папка — когда на ней не живёт ни одно задание.
"""

from collections import defaultdict
from datetime import timedelta
from functools import partial

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from attachments.storage import drop_prefix, file_storage
from attachments.uploads import under

from .models import PrintJob

Status = PrintJob.Status
KEEP = timedelta(days=1)
ROOT = "printing"


def _last(job):
    """Последнее, что с заданием происходило."""
    return max(moment for moment in (job.created, job.analyzed_at, job.submitted_at, job.finished_at, job.heard_at) if moment)


def sweep(apply=False, say=print):
    """Что пора убрать — убрать (apply) или только назвать. Возвращает (истекло, папок, бесхозных)."""
    cut = timezone.now() - KEEP
    # Ждёт выбора дольше суток — по обоим срокам: у копии «ещё раз» разбор старый.
    stale = PrintJob.objects.filter(status=Status.READY, created__lt=cut).filter(
        Q(analyzed_at__lt=cut) | Q(analyzed_at__isnull=True)
    )
    late = set(stale.values_list("pk", flat=True))
    expired = stale.update(status=Status.EXPIRED) if apply else len(late)

    folders = defaultdict(list)
    for job in PrintJob.objects.filter(files_dropped=False):
        folders[job.folder].append(job)
    done = sorted(
        folder for folder, jobs in folders.items()
        if all((job.status in PrintJob.FINAL or job.pk in late) and _last(job) < cut for job in jobs)
    )
    for folder in done:
        say(f"{folder} — задания закончены больше суток назад")
        if apply:
            _drop(folder, mark=True)

    stray = _strays(set(folders), cut)
    for folder in stray:
        say(f"{folder} — ни одного задания")
        if apply:
            _drop(folder)
    return expired, len(done), len(stray)


def _drop(folder, mark=False):
    """Снять папку: пометка — в транзакции, файлы — после коммита. Упало удаление — папку
    снимет следующая ночь (`_strays`)."""
    with transaction.atomic():
        if mark:
            PrintJob.objects.filter(source_key__startswith=folder).update(files_dropped=True)
        transaction.on_commit(partial(drop_prefix, folder))


def _strays(alive, cut):
    """Папки под printing/ без живого задания: неотправленная форма, удаление в админке,
    упавшая прошлая уборка. Свежие не трогаем — файл ещё ждёт свою форму."""
    storage = file_storage()
    try:
        names, _ = storage.listdir(ROOT)
    except (FileNotFoundError, NotADirectoryError):
        return []
    stray = []
    for name in sorted(names):
        folder = f"{ROOT}/{name}/"
        if folder in alive:
            continue
        keys = sorted(under(folder.rstrip("/")))
        if keys and storage.get_modified_time(keys[0]) < cut:
            stray.append(folder)
    return stray
