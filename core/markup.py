"""Markdown → безопасный HTML.

Текст материалов (а позже и комментариев) пишут люди, и Markdown пропускает сырой
HTML из исходника НАСКВОЗЬ. Поэтому чистим уже собранный HTML: иначе автор мог бы
положить <script> в свой материал и он выполнился бы у каждого читателя.
"""

import markdown
import nh3

TAGS = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "del", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
    "span", "div",  # только под формулы, см. ALLOWED_CLASSES
}
ATTRIBUTES = {"a": {"href", "title"}, "img": {"src", "alt", "title"}}
# class разрешён единственным значением: иначе автор мог бы навесить на свой материал
# любые утилиты Tailwind и, например, растянуть чёрный блок на весь экран.
ALLOWED_CLASSES = {"span": {"arithmatex"}, "div": {"arithmatex"}}

EXTENSIONS = [
    "extra",
    "sane_lists",
    # Одиночный перенос строки становится <br>: люди пишут конспект как в блокноте
    # и не ждут, что две строки склеятся в абзац.
    "nl2br",
    # Формулы. Разметку внутри $…$ markdown иначе испортит (подчёркивания станут
    # курсивом, слэши съедятся); arithmatex вынимает их до разбора и возвращает
    # обёрнутыми в \( \) — их и подхватывает KaTeX уже в браузере (core/js/math.js).
    "pymdownx.arithmatex",
]
EXTENSION_CONFIGS = {"pymdownx.arithmatex": {"generic": True}}


def render(text):
    if not text:
        return ""
    html = markdown.markdown(text, extensions=EXTENSIONS, extension_configs=EXTENSION_CONFIGS)
    return nh3.clean(html, tags=TAGS, attributes=ATTRIBUTES, allowed_classes=ALLOWED_CLASSES)
