"""Тексты материалов старого сайта → markdown.

Писали их в Quill, и в базе лежат две разные формы: сперва редактор хранил готовый
HTML, позже перешёл на собственный формат Delta. Приводим обе к markdown, на котором
живёт новый сайт (core/markup.py).

Формулы в обеих формах хранят исходный LaTeX — в Delta это вставка-объект, в HTML
атрибут data-value рядом с уже отрисованной вёрсткой KaTeX. Берём исходник и отдаём
как $…$: их подхватит arithmatex, а отрисовку выбрасываем.
"""

import json
import re
from html.parser import HTMLParser

# Атрибуты, которые в Delta висят на переносе строки и описывают весь абзац.
BLOCK_KEYS = ("code-block", "header", "blockquote", "list")
# Quill ставит его перед вставкой-объектом; в тексте он невидим и только мешает.
ZERO_WIDTH = "﻿"
# Экранируем то, что markdown иначе примет за разметку. $ не трогаем — на нём формулы.
SPECIAL = re.compile(r"([\\`*_\[\]])")
# Начало строки, которое markdown принял бы за список, заголовок или цитату.
LINE_START = re.compile(r"^(\s*)([-+>#]|\d+\.)(\s)")


def escape(text, starts_line=False):
    """starts_line важен: «- » в начале абзаца markdown примет за список, а в середине
    фразы это обычное тире, и экранировать его значит показать читателю «\\-»."""
    text = SPECIAL.sub(r"\\\1", text)
    return LINE_START.sub(r"\1\\\2\3", text) if starts_line else text


def wrap(text, left, right=None):
    """Обёртка без захвата пробелов: markdown не понимает «** текст **»."""
    body = text.strip()
    if not body:
        return text
    head = text[: len(text) - len(text.lstrip())]
    tail = text[len(text.rstrip()):]
    return f"{head}{left}{body}{right or left}{tail}"


def formula(latex):
    return f"${latex.strip()}$" if latex and latex.strip() else ""


# ── Delta ─────────────────────────────────────────────────────────────────────────


def inline(chunk, attrs):
    if attrs.get("code"):
        return wrap(chunk, "`")  # внутри кода разметки нет, дальше не оборачиваем
    for key, marker in (("bold", "**"), ("italic", "*"), ("strike", "~~")):
        if attrs.get(key):
            chunk = wrap(chunk, marker)
    if link := attrs.get("link"):
        chunk = f"[{chunk.strip()}]({link})"
    return chunk


def delta_lines(ops):
    """Delta хранит абзац так: сначала его содержимое, потом отдельная вставка «\\n»
    с атрибутами САМОГО абзаца. Поэтому строку закрываем на переносе — и только там
    узнаём, была она заголовком, пунктом списка или кодом."""
    line = []
    for op in ops:
        insert = op.get("insert")
        attrs = op.get("attributes") or {}
        if isinstance(insert, dict):
            line.append(formula(insert.get("formula", "")))
            continue
        if not isinstance(insert, str):
            continue

        block = {k: attrs[k] for k in BLOCK_KEYS if k in attrs}
        parts = insert.replace(ZERO_WIDTH, "").split("\n")
        for index, part in enumerate(parts):
            if part:
                line.append(inline(escape(part, starts_line=not line), attrs))
            if index < len(parts) - 1:
                yield "".join(line).strip(), block
                line = []
    if line:
        yield "".join(line).strip(), {}


def delta_to_markdown(raw):
    ops = json.loads(raw).get("ops", [])
    return assemble(delta_lines(ops))


# ── старый HTML ───────────────────────────────────────────────────────────────────


class HtmlLines(HTMLParser):
    """Собирает те же (строка, блок), что и разбор Delta, — дальше сборка общая.

    Основная работа — выкинуть вёрстку KaTeX: она занимает по килобайту на формулу,
    а нужный нам LaTeX лежит рядом, в data-value.
    """

    INLINE = {"strong": "**", "b": "**", "em": "*", "i": "*", "s": "~~", "del": "~~", "code": "`"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.line = []
        self.skip = 0  # глубина внутри отрисованной формулы
        self.list_kind = None
        self.pending = None
        self.link = None

    def flush(self, block=None):
        text = "".join(self.line).strip()
        if text:
            self.lines.append((text, block or {}))
        self.line = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.skip:
            self.skip += 1
            return
        if tag == "span" and "ql-formula" in (attrs.get("class") or ""):
            self.line.append(formula(attrs.get("data-value", "")))
            self.skip = 1
            return
        if tag in ("p", "div"):
            self.flush()
        elif tag == "br":
            self.flush()
        elif tag in ("ol", "ul"):
            self.flush()
            self.list_kind = "ordered" if tag == "ol" else "bullet"
        elif tag == "li":
            self.flush()
            self.pending = {"list": self.list_kind or "bullet"}
        elif tag == "a":
            self.link = attrs.get("href", "")
            self.line.append("[")
        elif marker := self.INLINE.get(tag):
            self.line.append(marker)

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= 1
            return
        if tag in ("p", "div", "li"):
            self.flush(self.pending)
            self.pending = None
        elif tag in ("ol", "ul"):
            self.flush()
            self.list_kind = None
        elif tag == "a":
            self.line.append(f"]({self.link or ''})")
            self.link = None
        elif marker := self.INLINE.get(tag):
            self.line.append(marker)

    def handle_data(self, data):
        if not self.skip:
            clean = data.replace(ZERO_WIDTH, "").replace("\xa0", " ")
            self.line.append(escape(clean, starts_line=not self.line))

    def result(self):
        self.flush()
        return self.lines


def html_to_markdown(raw):
    parser = HtmlLines()
    parser.feed(raw)  # сущности разворачивает сам парсер (convert_charrefs)
    return assemble(parser.result())


# ── общая сборка ──────────────────────────────────────────────────────────────────


def assemble(lines):
    """Строки с их блочными атрибутами → готовый markdown.

    Пункты списка идут вплотную, всё остальное разделяем пустой строкой: с sane_lists
    список без пустой строки перед ним не начнётся, а абзацы без неё склеятся в один.
    Подряд идущие строки кода собираем в одну ограду, а не в десяток однострочных.
    """
    out = []
    code = []
    previous = None

    def close_code():
        if code:
            out.append("```\n" + "\n".join(code) + "\n```")
            code.clear()

    for text, block in lines:
        if "code-block" in block:
            code.append(text)
            previous = "code"
            continue
        close_code()
        if not text:
            previous = None
            continue

        kind = "list" if "list" in block else "plain"
        if block.get("list") == "ordered":
            text = f"1. {text}"
        elif block.get("list") == "bullet":
            text = f"- {text}"
        elif level := block.get("header"):
            text = f"{'#' * min(int(level), 6)} {text}"
        elif "blockquote" in block:
            text = f"> {text}"

        if out and not (kind == "list" and previous == "list"):
            out.append("")
        out.append(text)
        previous = kind

    close_code()
    return "\n".join(out).strip()


def to_markdown(raw):
    """Пустой результат — нормальный: у большинства материалов в тексте только перенос."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return delta_to_markdown(raw) if raw.startswith("{") else html_to_markdown(raw)
    except (ValueError, KeyError):
        return ""
