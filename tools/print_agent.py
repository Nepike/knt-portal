#!/usr/bin/env python3
"""Агент печати студсайта: превращает загруженный документ в то, что ляжет на бумагу.

    python3 tools/print_agent.py analyze конспект.docx
    python3 tools/print_agent.py analyze слайды.pptx -o разбор --json

Договор целиком — в `docs/printing.md`. Коротко: сайт не открывает документы и не говорит
с принтером. Агент живёт на машине в сети М4, сам забирает задания с сайта и приносит назад
цифры — страницы и заливку — и превью.

Пока здесь только анализатор (этап 1): что именно напечатается и сколько тонера на это уйдёт.
Работает без сайта, на локальном файле — им проверяют, что превью совпадает с бумагой.

Как и пекарня, только стандартная библиотека и системный python3 (на М4 это 3.10, поэтому
без tomllib и прочего посвежее). Внешние вещи — программы: LibreOffice, Ghostscript, CUPS
(`cupsfilter`), poppler (`pdfinfo`) и bubblewrap. PPD очереди берётся у CUPS через pycups.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
    """JPEG в оттенках серого на каждую страницу print.pdf: pages/0001.jpg, 0002.jpg…"""
    (work / "pages").mkdir(exist_ok=True)
    result = sandbox(work, [
        *GS, "-sDEVICE=jpeggray", f"-r{PREVIEW_DPI}", "-dJPEGQ=70", "-o", "pages/%04d.jpg", "print.pdf",
    ], capture_output=True)
    if result.returncode != 0:
        raise Rejected("Не удалось нарисовать превью страниц. Попробуйте сохранить документ в PDF заново.")
    return sorted(path.name for path in (work / "pages").glob("*.jpg"))


def printer_ppd(queue):
    """PPD очереди у CUPS: по нему нормализация знает лист и печатное поле принтера."""
    import cups  # системный пакет python3-cups; нужен только здесь

    return cups.Connection().getPPD(queue)


def analyze(source, work, ppd):
    """Разобрать файл в папке work. Возвращает отчёт для сайта; отказ — исключением Rejected."""
    seconds = {}
    kind = accept(source)
    shutil.copyfile(source, work / f"source{kind}")
    shutil.copyfile(ppd, work / "printer.ppd")

    started = time.monotonic()
    pdf = to_pdf(work, kind)
    seconds["convert"] = took(started)

    started = time.monotonic()
    pages, sizes = pdf_info(work, pdf)
    if not pages:
        raise Rejected("В документе нет ни одной страницы.")
    normalize(work, pdf)
    printed, _ = pdf_info(work, "print.pdf")
    seconds["normalize"] = took(started)

    started = time.monotonic()
    shares = coverage(work)
    seconds["coverage"] = took(started)

    started = time.monotonic()
    pictures = previews(work)
    seconds["previews"] = took(started)

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
    args = parser.parse_args()
    return command_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
