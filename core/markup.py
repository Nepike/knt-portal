"""Markdown → безопасный HTML. Markdown пропускает сырой HTML насквозь, поэтому чистим
уже собранный HTML."""

import re
from urllib.parse import unquote, urlsplit
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
# class — только одно значение: иначе автор навесил бы любые утилиты Tailwind.
ALLOWED_CLASSES = {"span": {"arithmatex"}, "div": {"arithmatex"}}

EXTENSIONS = [
    "extra",
    "sane_lists",
    # Одиночный перенос — <br>: конспект пишут как в блокноте.
    "nl2br",
    # Формулы: arithmatex вынимает $…$ до разбора (иначе markdown их испортит) и оборачивает
    # в \( \) для KaTeX (core/js/math.js).
    "pymdownx.arithmatex",
]
EXTENSION_CONFIGS = {"pymdownx.arithmatex": {"generic": True}}
# Куда своя картинка может вести: раздача картинок (attachments.media_image) и статика.
IMAGE_PATHS = ("/img/", "/static/")


def _ours(url):
    """Своя картинка: адрес на нашем сайте и путь к картинке. Чужая выдала бы её хозяину
    каждого читателя. Своего домена мало: `![](/f/…)` накручивал бы скачивания (economy.rewards).
    """
    # Браузер выкидывает из адреса табы и переводы строк, а «\» читает как «/».
    url = re.sub(r"[\t\r\n]", "", url).strip().replace("\\", "/")
    if url.startswith("//"):
        return False
    parts = urlsplit(url)
    if not url.startswith("/") and not (parts.scheme in ("http", "https") and parts.hostname in settings.ALLOWED_HOSTS):
        return False
    # Путь — каким его поймёт браузер: «..» и «%2e%2e» он схлопывает, и /img/../f/… стал бы /f/….
    segments = unquote(parts.path).split("/")
    return parts.path.startswith(IMAGE_PATHS) and not {".", ".."} & set(segments)


class _ForeignImages(Treeprocessor):
    """Чужая картинка становится ссылкой на неё."""

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
