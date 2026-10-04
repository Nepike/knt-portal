"""Markdown → безопасный HTML.

Текст материалов (а позже и комментариев) пишут люди, и Markdown пропускает сырой
HTML из исходника НАСКВОЗЬ. Поэтому чистим уже собранный HTML: иначе автор мог бы
положить <script> в свой материал и он выполнился бы у каждого читателя.
"""

import re
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

import markdown
import nh3
from django.conf import settings
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

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


def _ours(url):
    """Адрес на нашем сайте. Чужая картинка грузится у читателя сама и выдаёт её хозяину
    адрес каждого, кто открыл материал, — поэтому в тексте остаются только свои."""
    # Браузер выкидывает из адреса табы и переводы строк, а «\» читает как «/».
    url = re.sub(r"[\t\r\n]", "", url).strip().replace("\\", "/")
    if url.startswith("/"):
        return not url.startswith("//")
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and parts.hostname in settings.ALLOWED_HOSTS


class _ForeignImages(Treeprocessor):
    """Чужая картинка становится ссылкой на неё: текст автора не пропадает, а грузить
    её читателю или нет — он решает сам."""

    def run(self, root):
        self.walk(root, linked=False)

    def walk(self, parent, linked):
        linked = linked or parent.tag == "a"
        for index, child in enumerate(list(parent)):
            if child.tag == "img" and not _ours(child.get("src", "")):
                # Картинка-ссылка: второй ссылки внутри первой не бывает, остаётся подпись.
                stub = Element("span") if linked else Element("a", href=child.get("src", ""))
                stub.text = child.get("alt") or child.get("src", "")
                stub.tail = child.tail
                parent[index] = stub
            else:
                self.walk(child, linked)


class _OwnImagesOnly(Extension):
    def extendMarkdown(self, md):
        # После разбора строчной разметки (приоритет 20): раньше картинок в дереве ещё нет.
        md.treeprocessors.register(_ForeignImages(md), "foreign_images", 5)


def _attribute(tag, name, value):
    # <img>, вписанный сырым HTML, в дерево markdown не попадает — у него снимаем адрес.
    if tag == "img" and name == "src" and not _ours(value):
        return None
    return value


def render(text):
    if not text:
        return ""
    html = markdown.markdown(
        text, extensions=[*EXTENSIONS, _OwnImagesOnly()], extension_configs=EXTENSION_CONFIGS,
    )
    return nh3.clean(
        html, tags=TAGS, attributes=ATTRIBUTES, allowed_classes=ALLOWED_CLASSES, attribute_filter=_attribute,
    )
