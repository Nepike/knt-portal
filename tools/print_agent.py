#!/usr/bin/env python3
"""Агент печати студсайта: превращает загруженный документ в то, что ляжет на бумагу, и печатает.

    python3 tools/print_agent.py analyze конспект.docx          разобрать файл без сайта
    python3 tools/print_agent.py analyze слайды.pptx -o разбор --json
    python3 tools/print_agent.py serve analyze                   служба: разбор файлов с сайта
    python3 tools/print_agent.py serve print                     служба: печать и показания принтеров

Договор целиком — в `docs/printing.md`. Коротко: сайт не открывает документы и не говорит
с принтером. Агент живёт на машине в сети М4, сам забирает задания с сайта, приносит назад
цифры — страницы и заливку — и превью, а потом печатает выбранное.

Служба — два процесса, а не потоки одного: разбор (LibreOffice над чужим файлом, до двух
минут) не должен задерживать печать, а остановка сигналом в однопоточном процессе так же
проста, как у пекарни (`tools/bake.py`). Юнит — `print-agent@.service`, экземпляр на каждый.

Как и пекарня, только стандартная библиотека и системный python3 (на М4 это 3.10, поэтому
без tomllib и прочего посвежее). Внешние вещи — программы: LibreOffice, Ghostscript, CUPS
(`cupsfilter`), poppler (`pdfinfo`), qpdf, snmpget и bubblewrap; с самим CUPS — через pycups.
Адрес сайта и токен — `PRINT_URL` и `PRINT_AGENT_TOKEN` в окружении или в `.env` репозитория.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # запускают файлом, а не модулем — корень сам не появится
    sys.path.insert(0, str(ROOT))

from printing.pricing import pages_of  # noqa: E402 — только после правки sys.path

QUEUE = "m4-kyocera"

MAX_FILE_MB = 50
# Предел документа больше недельного лимита: печатают часто одну главу из толстого учебника,
# а выбрать её можно только после превью.
MAX_DOC_PAGES = 300
# Студенческий DOCX на сотню страниц LibreOffice переводит секунд за двадцать. Две минуты —
# это уже зависший файл, а не долгий.
CONVERT_TIMEOUT = 120
STEP_TIMEOUT = 120

# Чем файл обязан начинаться. LibreOffice откроет что угодно — случайные байты под именем
# .docx он молча прочтёт как текст и напечатает страницу мусора, — поэтому сверяем сами.
# DOCX, PPTX, ODT и ODP — это zip; DOC и PPT — контейнер OLE.
ZIP, OLE = b"PK\x03\x04", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
SIGNATURES = {
    ".pdf": [b"%PDF"],
    ".docx": [ZIP], ".pptx": [ZIP], ".odt": [ZIP], ".odp": [ZIP],
    ".doc": [OLE], ".ppt": [OLE],
    ".rtf": [b"{\\rtf"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".jpg": [b"\xff\xd8\xff"], ".jpeg": [b"\xff\xd8\xff"],
}
OFFICE = {".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".odp"}
IMAGES = {".jpg", ".jpeg", ".png"}
ACCEPTED = set(SIGNATURES)
# PDF разрешено начинаться не с первого байта: перед %PDF бывает мусор, и читалки его прощают.
PDF_SLACK = 1024

# Заливку меряем на 100 dpi: точнее незачем, а страница A4 — это всего миллион точек.
COVERAGE_DPI = 100
# Превью шириной около 300 точек: сетка страниц на экране, а не чтение текста.
PREVIEW_DPI = 36
# А это — для просмотрщика: на 100 dpi обычный текст читается, и видно, не съехала ли
# вёрстка и не стоят ли квадратики вместо букв (шрифта, которого нет на М4).
LARGE_DPI = 100
# Страница с заливкой выше этой отмечается студенту: тёмный слайд стоит как пять текстовых.
DARK = 0.25


class Rejected(Exception):
    """Отказ, который увидит студент. Текст — человеческим языком и с тем, что делать."""


def say(text=""):
    print(text, file=sys.stderr, flush=True)


def took(started):
    return round(time.monotonic() - started, 2)


def jail(work, timeout=STEP_TIMEOUT):
    """Начало команды, которая запускает программу над чужим файлом: без сети и видя из
    файловой системы только папку задания (она же /work) и системные каталоги на чтение.

    Машина на М4 видит всю сеть факультета, включая роутер, а документ может ссылаться на
    картинку по адресу или нести в себе что похуже. --unshare-all отключает сеть и прочие
    общие пространства; --die-with-parent не оставит сироту, если агент упадёт; timeout
    внутри — чтобы зависшая программа умерла сама, а не держала задание.
    """
    return [
        "bwrap", "--unshare-all", "--die-with-parent", "--new-session",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
        "--ro-bind-try", "/var/cache/fontconfig", "/var/cache/fontconfig",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--bind", str(work), "/work", "--chdir", "/work",
        "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
        "timeout", "--kill-after=5", str(timeout),
    ]


def sandbox(work, argv, timeout=STEP_TIMEOUT, **kwargs):
    return subprocess.run([*jail(work, timeout), *argv], timeout=timeout + 15, **kwargs)


# Ghostscript со сглаживанием: тонкий текст на малом разрешении считается по площади,
# а не «ступеньками», — и для заливки, и для превью.
GS = ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4"]


def accept(source):
    """Проверка на входе — до того, как файл открыла хоть одна программа."""
    size = source.stat().st_size
    if size > MAX_FILE_MB * 1024 * 1024:
        raise Rejected(f"Файл больше {MAX_FILE_MB} МБ. Сожмите его или загрузите только нужную часть.")
    kind = source.suffix.lower()
    if kind not in ACCEPTED:
        raise Rejected(
            "Такой файл не печатаем. Подойдут PDF, DOC, DOCX, ODT, RTF, PPT, PPTX, ODP, JPG и PNG. "
            "Таблицы сохраните в PDF — так они напечатаются предсказуемо."
        )
    if size == 0:
        raise Rejected("Файл пустой.")
    with source.open("rb") as file:
        head = file.read(PDF_SLACK)
    known = SIGNATURES[kind]
    fits = any(sign in head for sign in known) if kind == ".pdf" else any(head.startswith(sign) for sign in known)
    if not fits:
        raise Rejected("Файл не открывается: он повреждён или это не тот формат, что в названии.")
    return kind


def pdf_info(work, name):
    """Страницы PDF: число и размер каждой, с учётом поворота. Пароль и битый файл — отказ."""
    result = sandbox(work, ["pdfinfo", "-f", "1", "-l", str(MAX_DOC_PAGES + 1), name],
                     capture_output=True, text=True)
    out = result.stdout
    if result.returncode != 0 or "Pages:" not in out:
        if "Incorrect password" in result.stderr or "encrypted" in result.stderr.lower():
            raise Rejected("PDF защищён паролем. Снимите защиту и загрузите снова.")
        raise Rejected("Файл не открывается: он повреждён или это не тот формат, что в названии.")
    fields = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
    # «Защищённый» PDF без пароля на открытие обычно запрещает лишь копирование — такой
    # печатаем. Отказываем, только если автор запретил именно печать.
    if "print:no" in fields.get("Encrypted", ""):
        raise Rejected("Автор PDF запретил его печать. Нужен экземпляр без этого запрета.")
    pages = int(fields["Pages"])
    if pages > MAX_DOC_PAGES:
        raise Rejected(
            f"В документе {pages} страниц, а берём не больше {MAX_DOC_PAGES}. "
            "Сохраните в PDF нужную главу и загрузите её."
        )
    sizes = []
    for number in range(1, pages + 1):
        size = fields.get(f"Page {number:>4} size") or fields.get(f"Page {number} size") or ""
        rotation = fields.get(f"Page {number:>4} rot") or fields.get(f"Page {number} rot") or "0"
        width, height = (float(x) for x in size.split("pts")[0].split(" x ")) if " x " in size else (0, 0)
        if int(float(rotation)) % 180:
            width, height = height, width
        sizes.append((width, height))
    return pages, sizes


def to_pdf(work, kind):
    """Исходник → source.pdf. PDF как есть; документы — LibreOffice; картинка — фильтром CUPS,
    который сам впишет её в лист."""
    if kind == ".pdf":
        return "source.pdf"
    if kind in IMAGES:
        result = sandbox(work, [
            "cupsfilter", "-p", "printer.ppd", "-m", "application/vnd.cups-pdf",
            "-o", "media=A4", "-o", "fit-to-page", f"source{kind}",
        ], capture_output=True)
        if result.returncode != 0 or not result.stdout.startswith(b"%PDF"):
            raise Rejected("Картинка не открывается: файл повреждён или это не тот формат, что в названии.")
        (work / "source.pdf").write_bytes(result.stdout)
        return "source.pdf"
    try:
        result = sandbox(work, [
            "soffice", "--headless", "--norestore", "--nolockcheck", "--nodefault",
            "-env:UserInstallation=file:///tmp/lo-profile",
            "--convert-to", "pdf", "--outdir", "/work", f"source{kind}",
        ], timeout=CONVERT_TIMEOUT, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        result = None
    if result is None or result.returncode == 124:
        raise Rejected(
            f"Не удалось открыть файл за {CONVERT_TIMEOUT // 60} минуты. "
            "Сохраните его в PDF у себя и загрузите PDF."
        )
    if not (work / "source.pdf").exists():
        raise Rejected("Файл не открывается: он повреждён или сохранён в незнакомом формате. Попробуйте PDF.")
    return "source.pdf"


def normalize(work, name):
    """source.pdf → print.pdf: каждая страница — ровно лист A4, как её и положит принтер.

    Тем же фильтром pdftopdf, что потом применит CUPS при печати, — поэтому превью и заливка
    считаются по тому, что выйдет на бумагу. Масштаб — по умолчанию фильтра: лист A4 остаётся
    в натуральную величину, A3, Letter и слайды 16:9 вписываются (альбомные поворачиваются
    поперёк листа). `fit-to-page` не берём: с ним ужимался бы и обычный A4 — на 7%, до
    печатного поля принтера, — и шрифт на бумаге выходил бы мельче, чем в документе.
    """
    result = sandbox(work, [
        "cupsfilter", "-p", "printer.ppd", "-m", "application/vnd.cups-pdf", "-o", "media=A4", name,
    ], capture_output=True)
    if result.returncode != 0 or not result.stdout.startswith(b"%PDF"):
        raise Rejected("Документ не удалось подготовить к печати. Попробуйте сохранить его в PDF заново.")
    (work / "print.pdf").write_bytes(result.stdout)


def _token(stream):
    """Следующее слово заголовка PGM: пропускаем пробелы и комментарии `#…`."""
    word = b""
    while True:
        byte = stream.read(1)
        if not byte:
            return word
        if byte == b"#" and not word:
            while byte not in (b"\n", b""):
                byte = stream.read(1)
            continue
        if byte.isspace():
            if word:
                return word
            continue
        word += byte


def gray_pages(stream):
    """Страницы из потока PGM (`P5`), который Ghostscript пишет подряд: байты яркости каждой.

    После числа 255 в заголовке идёт ровно один пробельный байт — его съедает _token.
    """
    while True:
        magic = _token(stream)
        if not magic:
            return
        if magic != b"P5":
            raise ValueError(f"не PGM: {magic[:10]!r}")
        width, height, top = int(_token(stream)), int(_token(stream)), int(_token(stream))
        if top != 255:
            raise ValueError(f"глубина {top}, ждали 255")
        size = width * height
        data = stream.read(size)
        if len(data) != size:
            raise ValueError("поток PGM оборвался посреди страницы")
        yield data


def darkness(data):
    """Доля тонера на странице: 1 − средняя яркость. Белая — 0, сплошь чёрная — 1.

    Монохромный принтер переводит цвет в серый так же, поэтому меряем серый, а не CMYK.
    `sum()` по bytes считается в C — numpy ради этого не нужен.
    """
    return 1 - sum(data) / (255 * len(data)) if data else 0.0


def coverage(work):
    """Заливка каждой страницы print.pdf. Ghostscript пишет страницы подряд в stdout, и мы
    читаем их по одной, не держа весь документ в памяти; свои сообщения он шлёт в stderr."""
    process = subprocess.Popen([
        *jail(work), *GS, "-sstdout=%stderr", "-sDEVICE=pgmraw", f"-r{COVERAGE_DPI}", "-o", "-", "print.pdf",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        shares = [round(darkness(page), 4) for page in gray_pages(process.stdout)]
    finally:
        process.stdout.close()
        process.wait(timeout=STEP_TIMEOUT)
    if process.returncode != 0:
        raise Rejected("Документ не удалось разобрать по страницам. Попробуйте сохранить его в PDF заново.")
    return shares


def previews(work):
    """JPEG в оттенках серого на каждую страницу print.pdf: pages/0001.jpg, 0002.jpg… —
    и крупные для просмотрщика в large/. Не вышли крупные — не беда: печатать можно и без
    них, просмотрщик покажет мелкие. Поэтому их сбой не отказ, а пустая папка."""
    (work / "pages").mkdir(exist_ok=True)
    result = sandbox(work, [
        *GS, "-sDEVICE=jpeggray", f"-r{PREVIEW_DPI}", "-dJPEGQ=70", "-o", "pages/%04d.jpg", "print.pdf",
    ], capture_output=True)
    if result.returncode != 0:
        raise Rejected("Не удалось нарисовать превью страниц. Попробуйте сохранить документ в PDF заново.")
    (work / "large").mkdir(exist_ok=True)
    large = sandbox(work, [
        *GS, "-sDEVICE=jpeggray", f"-r{LARGE_DPI}", "-dJPEGQ=75", "-o", "large/%04d.jpg", "print.pdf",
    ], capture_output=True)
    if large.returncode != 0:
        shutil.rmtree(work / "large", ignore_errors=True)
    return sorted(path.name for path in (work / "pages").glob("*.jpg"))


def printer_ppd(queue):
    """PPD очереди у CUPS: по нему нормализация знает лист и печатное поле принтера."""
    import cups  # системный пакет python3-cups; нужен только здесь

    return cups.Connection().getPPD(queue)


def analyze(source, work, ppd, beat=lambda: None):
    """Разобрать файл в папке work. Возвращает отчёт для сайта; отказ — исключением Rejected.

    beat зовётся между шагами: каждый шаг ограничен своим таймаутом, и весточки между
    ними хватает, чтобы сайт не счёл агента пропавшим (docs/printing.md, «Очередь»).
    """
    seconds = {}
    kind = accept(source)
    shutil.copyfile(source, work / f"source{kind}")
    shutil.copyfile(ppd, work / "printer.ppd")

    started = time.monotonic()
    pdf = to_pdf(work, kind)
    seconds["convert"] = took(started)
    beat()

    started = time.monotonic()
    pages, sizes = pdf_info(work, pdf)
    if not pages:
        raise Rejected("В документе нет ни одной страницы.")
    normalize(work, pdf)
    printed, _ = pdf_info(work, "print.pdf")
    seconds["normalize"] = took(started)
    beat()

    started = time.monotonic()
    shares = coverage(work)
    seconds["coverage"] = took(started)
    beat()

    started = time.monotonic()
    pictures = previews(work)
    seconds["previews"] = took(started)
    beat()

    if not (printed == len(shares) == len(pictures)):
        raise Rejected("Страницы документа посчитались по-разному. Попробуйте сохранить его в PDF заново.")
    return {
        "pages": printed,
        "coverage": shares,
        # Альбомная страница ложится поперёк листа A4: превью её покажет боком, студенту
        # надо это сказать словами.
        "sideways": [number for number, (width, height) in enumerate(sizes, 1) if width > height],
        "previews": pictures,
        "seconds": seconds,
    }


# ── Служба: разговор с сайтом ─────────────────────────────────────────────────

ANALYZE_EVERY = 3  # секунд между вопросами «есть работа?», пока её нет
PRINT_EVERY = 2
STATUS_IDLE = 60  # показания принтеров, пока печати нет
# …и во время неё: студенту «нет бумаги» нужно сразу, а не через минуту.
STATUS_BUSY = 10
ALIVE_EVERY = 30
CUPS_EVERY = 2
COUNTER_EVERY = 5
# После «выполнено» в CUPS ждём прироста счётчика. Столько секунд без прироста и без
# неполадки на принтере — и задание «неизвестно»: листов, похоже, не будет.
COUNTER_PATIENCE = 180
CHUNK = 1024 * 1024
PUT_TRIES = 4
UPLOAD_THREADS = 8
CONFIG_EVERY = 600
STOP_SIGNALS = [name for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)]
PRINT_PDF, BLANK_PDF = "print.pdf", "blank.pdf"


class Gone(Exception):
    """Сайт закрыл задание или отдал его другому — работу бросаем молча."""


class SiteError(Exception):
    """Сайт ответил отказом. Текст — причина с той стороны."""


def dotenv():
    """Строки из `.env` репозитория, как у пекарни: значение как есть, вместе с хвостом
    после решётки — django-environ на сайте его тоже не срезает."""
    path = ROOT / ".env"
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def setting(name, default=""):
    return os.environ.get(name) or dotenv().get(name, default)


class Site:
    """Ручки агента на сайте (`printing/agent.py`)."""

    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def talk(self, door, body=None):
        """GET без тела, POST с телом. Ответ — JSON; 409 — Gone; прочий отказ — SiteError."""
        request = urllib.request.Request(
            f"{self.url}/printing/agent/{door}/",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as answer:
                return json.load(answer)
        except urllib.error.HTTPError as error:
            # Причину сайт пишет в JSON, но 502 от nginx приезжает страницей — без разбора
            # в лоб вместо «сайт ответил 502» было бы JSONDecodeError.
            try:
                data = json.loads(error.read() or b"{}")
            except ValueError:
                data = {}
            detail = data.get("error", "") if isinstance(data, dict) else ""
            if error.code == 409:
                raise Gone(detail) from error
            raise SiteError(f"сайт ответил {error.code}: {detail or error.reason}") from error

    def tell(self, door, body):
        """Доложить, не падая: отчёт чаще всего нужен ровно тогда, когда связи и нет.
        Не дошло — не беда: замолчавшее задание сайт решит сам (docs/printing.md)."""
        try:
            return self.talk(door, body)
        except (Gone, SiteError, OSError, ValueError) as error:
            say(f"  ! {door}: сайту не сказалось ({error})")
            return None


class Beat:
    """Весточка сайту, что задание не брошено. Стучит сама работа — между шагами разбора
    и из циклов ожидания печати, — а не таймер: повисшая машина задание не удержит."""

    def __init__(self, site, token, every=ALIVE_EVERY):
        self.site, self.token, self.every = site, token, every
        self.last = time.monotonic()

    def __call__(self, panel=None):
        if time.monotonic() - self.last < self.every:
            return
        self.last = time.monotonic()
        body = {"token": self.token}
        if panel is not None:
            body["panel"] = panel
        self.site.tell("alive", body)


def download(url, target, limit=None):
    """Файл задания прямо из хранилища, потоком. Больше limit байт — отказ студенту."""
    size = 0
    with urllib.request.urlopen(url, timeout=120) as answer, target.open("wb") as out:
        while chunk := answer.read(CHUNK):
            size += len(chunk)
            if limit is not None and size > limit:
                raise Rejected(f"Файл больше {limit // 1024 // 1024} МБ. Сожмите его или загрузите только нужную часть.")
            out.write(chunk)
    return size


def put_file(url, path, content_type=None):
    """Положить файл по подписанной ссылке, с повторами. Тип — тот, что назвал сайт:
    он записывается в объект и потом уходит браузеру (attachments.storage.CONTENT_TYPES)."""
    body = path.read_bytes()
    headers = {"Content-Type": content_type} if content_type else {}
    for attempt in range(1, PUT_TRIES + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="PUT")
            with urllib.request.urlopen(request, timeout=120):
                return
        except Exception as error:  # noqa: BLE001 — сеть отказывает десятком разных способов
            if attempt == PUT_TRIES:
                raise OSError(f"{path.name} не залился за {PUT_TRIES} попытки: {error}") from error
            time.sleep(2 ** attempt)


# ── Служба: разбор ────────────────────────────────────────────────────────────

def analysis_once(site, limit):
    """Взять один файл и разобрать. False — работы не было или связь оборвалась."""
    job = site.talk("claim", {"kind": "analyze"}).get("job")
    if not job:
        return False
    say(f"разбор {job['uuid'][:8]} · {job['name']}")
    started = time.monotonic()
    beat = Beat(site, job["token"])
    temp = Path(tempfile.mkdtemp(prefix="print-"))
    ppd = None
    try:
        (temp / "in").mkdir()
        work = temp / "work"
        work.mkdir()
        source = temp / "in" / f"source{job['extension']}"
        try:
            download(job["source"], source, limit)
            beat()
            ppd = printer_ppd(job["queue"])
            report = analyze(source, work, ppd, beat)
        except Rejected as refusal:
            say(f"  отказ: {refusal}")
            site.tell("fail", {"token": job["token"], "error": str(refusal)})
            return True
        answer = site.talk("analyzed", {
            "token": job["token"], "pages": report["pages"],
            "coverage": report["coverage"], "sideways": report["sideways"],
        })
        # Крупные превью сайт просит отдельно и заливаются они, только если нарисовались.
        urls = {**answer["urls"], **{
            name: url for name, url in (answer.get("large") or {}).items() if (work / name).exists()
        }}
        types = answer.get("types") or {}
        with ThreadPoolExecutor(max_workers=UPLOAD_THREADS) as pool:
            list(pool.map(lambda name: put_file(urls[name], work / name, types.get(name)), urls))
        site.talk("commit", {"token": job["token"]})
        say(f"  готово: {report['pages']} стр. за {took(started)} с")
    except Gone:
        say("  задание закрыто на сайте — бросаю")
    except KeyboardInterrupt:
        # Остановка — не отказ: разобрать файл по-прежнему можно, просто не сейчас.
        say("  остановлено — возвращаю задание в очередь")
        site.tell("release", {"token": job["token"]})
        raise
    except Exception as error:  # noqa: BLE001 — связь, хранилище, сбой агента: файл тут ни при чём
        # Не `fail`: студент увидел бы «не принято» из-за нашей поломки. Возвращаем в очередь,
        # а если ломается на нём раз за разом — сайт сам закроет его после пятой выдачи.
        say(f"  не вышло: {type(error).__name__}: {error}")
        site.tell("release", {"token": job["token"]})
        return False
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        if ppd:
            Path(ppd).unlink(missing_ok=True)
    return True


# ── Служба: печать ────────────────────────────────────────────────────────────

def assembly(selection, total, copies, duplex):
    """Что склеить для CUPS: [(файл, страницы), …] — выбранные страницы copies раз подряд.

    Копии собираем сами, а не отдаём CUPS: так однозначно, что выйдет. При двусторонней
    печати каждая копия начинается с нового листа — нечётной копии нужна пустая страница
    вслед, иначе следующая копия легла бы ей на оборот.
    """
    numbers = pages_of(selection, total)
    pad = duplex and len(numbers) % 2 == 1
    parts = []
    for copy in range(copies):
        parts.append((PRINT_PDF, selection))
        if pad and copy < copies - 1:
            parts.append((BLANK_PDF, "1"))
    return parts


def assemble(work, name, selection, total, copies, duplex):
    """print.pdf из папки work → name: выбранное, копиями, с пустыми страницами где надо."""
    parts = assembly(selection, total, copies, duplex)
    if any(file == BLANK_PDF for file, _ in parts):
        result = sandbox(work, [
            "gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
            "-sPAPERSIZE=a4", "-dFIXEDMEDIA", "-o", BLANK_PDF, "-c", "showpage",
        ], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError("не вышла пустая страница")
    pages = [value for part in parts for value in part]
    result = sandbox(work, ["qpdf", "--empty", "--pages", *pages, "--", name], capture_output=True, text=True)
    if result.returncode not in (0, 3):  # 3 — предупреждения: файл собран
        raise RuntimeError(f"qpdf: {result.stderr.strip()[:200]}")


# Printer MIB и Host Resources MIB. Индексы проверены на Kyocera M2535dn 22 окт 2026:
# лоток .2 — кассета (обходной .1 пуст всегда, это норма), «нет бумаги» — по ней и по битам.
OIDS = {
    "status": "1.3.6.1.2.1.25.3.5.1.1.1",
    "errors": "1.3.6.1.2.1.25.3.5.1.2.1",
    "counter": "1.3.6.1.2.1.43.10.2.1.4.1.1",
    "toner": "1.3.6.1.2.1.43.11.1.1.9.1.1",
    "capacity": "1.3.6.1.2.1.43.11.1.1.8.1.1",
    "cassette": "1.3.6.1.2.1.43.8.2.1.10.1.2",
    "panel": "1.3.6.1.2.1.43.16.5.1.2.1.1",
}
# hrPrinterDetectedErrorState, первый байт — биты со старшего: мало бумаги, нет бумаги,
# мало тонера, нет тонера, открыта дверца, замятие, офлайн, нужно обслуживание.
NO_PAPER, NO_TONER, DOOR, JAM, OFFLINE = 0x40, 0x10, 0x08, 0x04, 0x02
# Пока принтер говорит одно из этого, печать ждёт, а не сдаётся.
TROUBLE = {"no_paper", "jam", "open", "no_toner", "offline"}


def _number(text):
    try:
        return int(text.strip())
    except ValueError:
        return None


def _octets(text):
    """Строка `-Ox`: «"53 6C 65 "» → байты."""
    return bytes(int(pair, 16) for pair in re.findall(r"\b[0-9A-Fa-f]{2}\b", text))


def reading(lines):
    """Ответ snmpget (строки в порядке OIDS) → показание на языке сайта (Printer.State)."""
    values = dict(zip(OIDS, lines))
    status = _number(values.get("status", ""))
    errors = _octets(values.get("errors", ""))
    first = errors[0] if errors else 0
    level, capacity = _number(values.get("toner", "")), _number(values.get("capacity", ""))
    counter = _number(values.get("counter", ""))
    if first & JAM:
        state = "jam"
    elif first & DOOR:
        state = "open"
    elif first & NO_PAPER or _number(values.get("cassette", "")) == 0:
        state = "no_paper"
    elif first & NO_TONER:
        state = "no_toner"
    elif first & OFFLINE:
        state = "offline"
    elif status == 4:
        state = "busy"
    elif status in (1, 3, 5):  # «другое» — так он отвечает во сне; 5 — прогрев
        state = "ready"
    else:
        state = "unknown"
    return {
        "state": state,
        "panel": _octets(values.get("panel", "")).decode("utf-8", "replace").strip(" \x00"),
        # уровень −2 и −3 — «неизвестно» и «что-то есть»: процента из них не выйдет
        "toner": round(level * 100 / capacity) if level is not None and level >= 0 and capacity else None,
        "counter": counter if counter is not None and counter >= 0 else None,
    }


def snmp_read(address, community):
    """Показание принтера или None, если он не ответил. `-Ox` — строки шестнадцатеричными:
    иначе байт ошибок 0x40 snmpget напечатал бы символом «@», и биты пришлось бы угадывать."""
    try:
        result = subprocess.run(
            ["snmpget", "-v2c", "-c", community, "-t", "2", "-r", "1", "-Oqv", "-Ox", address, *OIDS.values()],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return reading(result.stdout.splitlines()) if result.returncode == 0 else None


class Monitor:
    """Показания всех принтеров — сайту, раз в STATUS_IDLE, а во время печати чаще."""

    def __init__(self, site, printers, community):
        self.site, self.printers, self.community = site, printers, community
        self.next = 0

    def __call__(self, busy=False):
        if time.monotonic() < self.next:
            return
        self.next = time.monotonic() + (STATUS_BUSY if busy else STATUS_IDLE)
        items = []
        for printer in self.printers.values():
            got = snmp_read(printer["address"], self.community)
            items.append({"id": printer["id"], **(got or {"state": "offline"})})
        if items:
            self.site.tell("status", {"printers": items})


class Journal:
    """Журнал против двойной печати: что отдаётся в CUPS прямо сейчас и что уже отдано.

    Пишется ДО отправки и снимается только после ответа сайта. Печать — единственный шаг,
    который нельзя повторить безопасно, и после перезапуска агент сначала разбирает журнал
    (`recover`), а не берёт новую работу. Запись — через временный файл и rename: оборванная
    на полуслове, она испортила бы журнал целиком.
    """

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def put(self, uuid, entry):
        data = self.load()
        data[uuid] = entry
        self._write(data)

    def drop(self, uuid):
        data = self.load()
        data.pop(uuid, None)
        self._write(data)

    def _write(self, data):
        temp = self.path.with_name(self.path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, self.path)


class Cups:
    """То немногое, что агенту нужно от CUPS. Отдельным классом — в тестах его подменяют.
    Соединение каждый раз новое: cupsd перезапускают, и старое после этого мертво."""

    def __init__(self):
        import cups  # системный пакет python3-cups

        self.cups = cups

    def last(self):
        """Номер последнего задания в CUPS — от него потом ищется наше."""
        return max(self.cups.Connection().getJobs(which_jobs="all"), default=0)

    def submit(self, queue, path, duplex):
        options = {"sides": "two-sided-long-edge" if duplex else "one-sided", "media": "A4"}
        return self.cups.Connection().printFile(queue, str(path), path.stem, options)

    def state(self, job):
        """(состояние IPP, текст принтера). Задания в CUPS нет — (None, "")."""
        try:
            attributes = self.cups.Connection().getJobAttributes(job)
        except self.cups.IPPError:
            return None, ""
        return attributes.get("job-state"), attributes.get("job-printer-state-message") or ""

    def find(self, name, after):
        """Задание, отправленное после after, с файлом name, или None.

        По имени файла, а не задания: имя задания CUPS прячет даже от владельца
        (JobPrivateValues), а `document-name-supplied` видно (проверено на М4 22 окт 2026).
        """
        connection = self.cups.Connection()
        for job in sorted(connection.getJobs(which_jobs="all")):
            if job > after and connection.getJobAttributes(job).get("document-name-supplied") == name:
                return job
        return None


def wait_counter(read, before, expected, beat, clock=time.monotonic, sleep=time.sleep, patience=COUNTER_PATIENCE):
    """Дождаться, пока счётчик оттисков вырастет на expected. True — вырос, False — встал.

    «Выполнено» в CUPS значит «принтер принял данные», а не «лист вышел»: в эмуляции PCL6
    (22 окт 2026) задания закрывались выполненными с нулём страниц. Пока принтер говорит
    о неполадке — бумага, замятие, крышка, — ждём сколько угодно: задание допечатается,
    когда починят. Счётчик растёт хотя бы на одну — тоже ждём дальше.
    """
    if before is None:
        return False
    seen, moved = before, clock()
    while True:
        got = read()
        count = got.get("counter") if got else None
        if count is not None:
            if count - before >= expected:
                return True
            if count > seen:
                seen, moved = count, clock()
        if got and got.get("state") in TROUBLE:
            moved = clock()
        elif clock() - moved > patience:
            return False
        beat(got.get("panel") if got else None)
        sleep(COUNTER_EVERY)


def finish(site, cups, journal, uuid, entry, read, monitor, clock=time.monotonic, sleep=time.sleep):
    """Довести отданное в CUPS: дождаться его, потом счётчика, и доложить сайту.

    Из журнала запись уходит только после ответа сайта — или когда сайт сказал, что задание
    уже не его (Gone). Не ответил — останется и доложится на следующем круге (`recover`).
    """
    token = entry["token"]
    beat = Beat(site, token)
    site.tell("sent", {"token": token, "cups_job": entry["cups_job"]})
    try:
        while True:
            state, message = cups.state(entry["cups_job"])
            if state in (7, 8):  # отменено, прервано
                say(f"  CUPS: не вышло — {message or 'без объяснений'}")
                site.talk("fail", {"token": token, "error": message or "принтер не принял задание"})
                journal.drop(uuid)
                return
            if state in (9, None):  # выполнено (или уже вычищено из истории)
                break
            monitor(busy=True)
            beat()
            sleep(CUPS_EVERY)

        def watched():
            monitor(busy=True)
            return read()

        sure = wait_counter(watched, entry.get("before"), entry["expected"], beat, clock, sleep)
        say(f"  {'напечатано' if sure else 'счётчик не сдвинулся — статус неизвестен'}")
        site.talk("printed", {"token": token, "sure": sure})
    except Gone:
        pass
    journal.drop(uuid)


def recover(site, cups, journal, read, monitor):
    """Разобрать журнал после перезапуска — до того, как брать новую работу.

    «Отдано» — довести (`finish`). «Отдаётся» — агент умер посреди отправки: ищем задание
    в CUPS; нашлось — довести, нет — до принтера не дошло, задание возвращается в очередь.
    """
    for uuid, entry in list(journal.load().items()):
        if entry["state"] == "sending":
            found = cups.find(entry["name"], entry.get("after", 0))
            if found is None:
                say(f"журнал: {uuid[:8]} до CUPS не дошло — возвращаю в очередь")
                site.tell("release", {"token": entry["token"]})
                journal.drop(uuid)
                continue
            entry.update(state="sent", cups_job=found)
            journal.put(uuid, entry)
        say(f"журнал: довожу {uuid[:8]} (CUPS {entry['cups_job']})")
        finish(site, cups, journal, uuid, entry, lambda e=entry: read(e), monitor)


def print_once(site, cups, journal, printers, read, monitor):
    """Взять одно задание и отдать в CUPS. False — работы не было или не вышло отдать."""
    job = site.talk("claim", {"kind": "print"}).get("job")
    if not job:
        return False
    uuid, token = job["uuid"], job["token"]
    say(f"печать {uuid[:8]} · стр. {job['selection']} × {job['copies']}{' · с двух сторон' if job['duplex'] else ''}")
    printer = printers.get(job["queue"]) or {}
    temp = Path(tempfile.mkdtemp(prefix="print-"))
    entry = None
    try:
        download(job["pdf"], temp / PRINT_PDF)
        name = f"knt-{uuid}.pdf"
        assemble(temp, name, job["selection"], job["pages"], job["copies"], job["duplex"])
        entry = {
            "token": token, "queue": job["queue"], "address": printer.get("address"), "name": name,
            "expected": job["sides"], "after": cups.last(), "state": "sending",
        }
        entry["before"] = (read(entry) or {}).get("counter")
        journal.put(uuid, entry)
        entry["cups_job"] = cups.submit(job["queue"], temp / name, job["duplex"])
        entry["state"] = "sent"
        journal.put(uuid, entry)
        say(f"  отдано в CUPS: задание {entry['cups_job']}")
    except Gone:
        say("  задание закрыто на сайте — бросаю")
        return True
    except KeyboardInterrupt:
        # До журнала — вернуть в очередь. После — молчать: что с отправкой, решит `recover`
        # при следующем старте, иначе остановка посреди отправки дала бы двойную печать.
        if uuid not in journal.load():
            say("  остановлено до отправки — возвращаю задание в очередь")
            site.tell("release", {"token": token})
        raise
    except Exception as error:  # noqa: BLE001 — сеть, хранилище, CUPS
        say(f"  не вышло: {type(error).__name__}: {error}")
        if uuid in journal.load():
            recover(site, cups, journal, read, monitor)  # отправка могла и уйти — решит поиск в CUPS
        else:
            site.tell("release", {"token": token})
        return False
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    finish(site, cups, journal, uuid, entry, lambda: read(entry), monitor)
    return True


# ── Служба: запуск ────────────────────────────────────────────────────────────

def catch_stop():
    """Остановка сигналом — то же, что Ctrl+C: задание возвращается в очередь как надо."""
    def stop(number, _frame):
        raise KeyboardInterrupt(signal.Signals(number).name)

    for name in STOP_SIGNALS:
        signal.signal(getattr(signal, name), stop)


def serve(kind):
    url, token = setting("PRINT_URL"), setting("PRINT_AGENT_TOKEN")
    if not url or not token:
        raise SystemExit("для службы нужны PRINT_URL и PRINT_AGENT_TOKEN (окружение или .env)")
    site = Site(url, token)
    catch_stop()
    try:
        return serve_analysis(site) if kind == "analyze" else serve_print(site)
    except KeyboardInterrupt:
        say("агент остановлен")
        return 0


def serve_analysis(site):
    global MAX_FILE_MB, MAX_DOC_PAGES
    limits = site.talk("config")["limits"]
    MAX_FILE_MB, MAX_DOC_PAGES = limits["max_file_mb"], limits["max_doc_pages"]
    say(f"агент разбирает файлы: {site.url}")
    while True:
        try:
            worked = analysis_once(site, MAX_FILE_MB * 1024 * 1024)
        except (Gone, SiteError, OSError, ValueError) as error:
            say(f"  сайт молчит: {error}")
            worked = False
        time.sleep(1 if worked else ANALYZE_EVERY)


def serve_print(site):
    community = setting("SNMP_COMMUNITY", "public")
    journal = Journal(Path(os.environ.get("STATE_DIRECTORY") or "/var/lib/print-agent") / "journal.json")
    cups = Cups()
    say(f"агент печатает: {site.url}, журнал {journal.path}")
    printers, refreshed = {}, 0
    monitor = Monitor(site, printers, community)

    def read(entry):
        return snmp_read(entry["address"], community) if entry.get("address") else None

    while True:
        try:
            if time.monotonic() - refreshed > CONFIG_EVERY:
                config = site.talk("config")
                printers = {p["queue"]: p for p in config["printers"] if p["active"]}
                monitor.printers = printers
                refreshed = time.monotonic()
            if journal.load():
                recover(site, cups, journal, read, monitor)
            monitor()
            worked = print_once(site, cups, journal, printers, read, monitor)
        except (Gone, SiteError, OSError, ValueError) as error:
            say(f"  сайт молчит: {error}")
            worked = False
        time.sleep(1 if worked else PRINT_EVERY)


def describe(name, report):
    """Отчёт человеку в консоль: сколько страниц, где тёмные, сколько ушло времени."""
    shares = report["coverage"]
    say(f"{name}: страниц {report['pages']}, заливка в среднем {sum(shares) / len(shares):.1%}")
    if len(shares) <= 20:
        say("  по страницам: " + " ".join(f"{number}:{share:.0%}" for number, share in enumerate(shares, 1)))
    else:
        say(f"  по страницам: от {min(shares):.0%} до {max(shares):.0%}")
    dark = [number for number, share in enumerate(shares, 1) if share > DARK]
    if dark:
        say(f"  тёмные (больше {DARK:.0%}): {', '.join(map(str, dark))}")
    if report["sideways"]:
        say(f"  поперёк листа: {', '.join(map(str, report['sideways']))}")
    say("  секунды: " + ", ".join(f"{step} {value}" for step, value in report["seconds"].items()))


def command_analyze(args):
    source = Path(args.file)
    ppd = args.ppd or printer_ppd(args.queue)
    with tempfile.TemporaryDirectory(prefix="print-") as temp:
        work = Path(temp)
        started = time.monotonic()
        try:
            report = analyze(source, work, ppd)
        except Rejected as refusal:
            say(f"{source.name}: отказ — {refusal}")
            if args.json:
                print(json.dumps({"rejected": str(refusal)}, ensure_ascii=False))
            return 2
        report["seconds"]["total"] = took(started)
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / "print.pdf", out / "print.pdf")
            shutil.copytree(work / "pages", out / "pages", dirs_exist_ok=True)
        describe(source.name, report)
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Агент печати студсайта")
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("analyze", help="разобрать локальный файл без сайта")
    one.add_argument("file")
    one.add_argument("-o", "--out", help="куда сложить print.pdf и превью")
    one.add_argument("--queue", default=QUEUE, help=f"очередь CUPS, чей PPD брать (по умолчанию {QUEUE})")
    one.add_argument("--ppd", help="взять PPD из файла, а не у CUPS")
    one.add_argument("--json", action="store_true", help="напечатать отчёт для сайта в stdout")
    service = commands.add_parser("serve", help="служба: забирать работу с сайта")
    service.add_argument("kind", choices=["analyze", "print"], help="разбор файлов или печать")
    args = parser.parse_args()
    if args.command == "serve":
        return serve(args.kind)
    return command_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
