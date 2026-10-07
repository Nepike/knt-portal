"""Ночной бэкап базы: дамп `pg_dump` в хранилище, под `backups/`.

Запускает его расписание (`CELERY_BEAT_SCHEDULE`), руками — `manage.py backup`.
Как из дампа восстановиться — в README, раздел про бэкапы.

Лежит в том же бакете, что и файлы сайта, — так решено: отдельный бакет со своим
ключом надёжнее, но это ещё одна вещь, за которой надо следить. Наружу папка не
торчит: раздача отдаёт только то, чей ключ подписало приложение.
"""

import os
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from django.core.files import File
from django.db import connection
from django.utils import timezone

from attachments.storage import file_storage
from attachments.uploads import listed

PREFIX = "backups"
# Две недели — чтобы заметить порчу, которая всплыла не сразу, и откатиться на день до неё.
KEEP_DAILY = 14
# И два месяца воскресных — на случай, когда и двух недель оказалось мало.
KEEP_WEEKLY = 8
NAME = re.compile(r"^knt-(\d{4})-(\d{2})-(\d{2})\.dump$")


class BackupError(Exception):
    pass


def key_for(day):
    return f"{PREFIX}/knt-{day:%Y-%m-%d}.dump"


def make():
    """Снять дамп и положить в хранилище. → (ключ, размер в байтах)."""
    db = connection.settings_dict
    if connection.vendor != "postgresql":
        raise BackupError("Бэкап снимается только с Postgres.")

    with tempfile.TemporaryDirectory() as room:
        path = Path(room) / "knt.dump"
        command = [
            # custom — сжатый формат, из которого pg_restore достаёт и всё, и одну таблицу.
            # Без владельцев и прав: восстановить можно под любой ролью.
            "pg_dump", "--format=custom", "--no-owner", "--no-privileges", f"--file={path}",
            f"--username={db['USER']}", f"--port={db['PORT'] or 5432}",
        ]
        if db["HOST"]:
            command.append(f"--host={db['HOST']}")
        # Пароль — окружением, а не аргументом: аргументы видны в списке процессов.
        result = subprocess.run(
            [*command, db["NAME"]], env={**os.environ, "PGPASSWORD": db["PASSWORD"]},
            capture_output=True, text=True,
        )
        if result.returncode:
            raise BackupError(f"pg_dump не справился: {result.stderr.strip()[-500:]}")

        storage, key = file_storage(), key_for(timezone.localdate())
        # Второй запуск за день заменяет первый, а не кладёт рядом: имя — это дата.
        if storage.exists(key):
            storage.delete(key)
        with path.open("rb") as body:
            storage.save(key, File(body))
        return key, path.stat().st_size


def stored():
    """Что лежит в хранилище: {дата: (ключ, размер)}. Чужие файлы в папке не в счёт."""
    found = {}
    for key, (_, size) in listed(PREFIX).items():
        if match := NAME.match(key.removeprefix(f"{PREFIX}/")):
            found[date(*map(int, match.groups()))] = (key, size)
    return found


def prune():
    """Снять лишние дампы: остаются KEEP_DAILY последних и KEEP_WEEKLY последних воскресных.

    Считаем по тому, что лежит, а не по календарю: пропущенная ночь не должна съесть
    бэкап постарше. Трогаем только свои имена — всё прочее в папке не наше.
    """
    found = stored()
    days = sorted(found, reverse=True)
    sundays = [day for day in days if day.isoweekday() == 7]
    keep = set(days[:KEEP_DAILY]) | set(sundays[:KEEP_WEEKLY])

    storage, gone = file_storage(), []
    for day in days:
        if day not in keep:
            storage.delete(found[day][0])
            gone.append(found[day][0])
    return gone
