import base64
import json
import logging

from django import template
from django.contrib.auth import get_user_model
from django.core.cache import cache

from core.models import Moderated
from core.templatetags.text_extras import plural
from library.models import Book

from ..models import CLUES_CACHE, Clue, Find, Fragment

register = template.Library()
logger = logging.getLogger(__name__)
PLACES = ("log", "card", "devtools", "notfound", "denied")


def clues():
    """Тексты подсказок по местам (кроме консоли — та только через quest.views.ask) и число
    фрагментов. В кэше пять минут; правка в админке сбрасывает."""
    def load():
        found = {place: [] for place in PLACES}
        for place, text in Clue.objects.filter(active=True).exclude(place=Clue.Place.COMMAND).values_list("place", "text"):
            found[place].append(text)
        found["fragments"] = Fragment.objects.count()
        return found
    return cache.get_or_set(CLUES_CACHE, load, 300)


def counts():
    """Настоящие числа для журнала — сколько книг и людей. Раз в десять минут хватит."""
    def load():
        return {
            "books": Book.objects.filter(status=Moderated.Status.APPROVED).count(),
            "people": get_user_model().objects.filter(is_active=True).count(),
        }
    return cache.get_or_set("quest:journal-counts", load, 600)


# Журнал сменяет строку раз в 1,7 с: строк — чтобы подсказка мелькала не всё время, но раз в минуту.
JOURNAL_STEP = 1.7
ROUND = 60


@register.inclusion_tag("quest/_journal.html", takes_context=True)
def journal(context):
    """Журнал взлома: выдуманные строки, настоящие числа и своё имя — чужого нет. Между строк —
    подсказки и свой счёт фрагментов. С какой строки начать, решает браузер."""
    request = context["request"]
    known = counts()
    found = clues()
    lines = [
        "GET /materials … 200",
        "ssh root@knt-mipt.ru … ok",
        f"копирование /library … {known['books']} {plural(known['books'], 'книга,книги,книг')}",
        "nmap knt-mipt.ru … 443 открыт, 80 тоже",
        "индексация /lectorium … ok",
        "/lectorium/*.m3u8 … смотрим лекции на 2×",
        f"сессий под наблюдением: {known['people']}",
        "подбор пароля к /pulse … 3%",
        f"{request.user.full_name} → {request.path}",
        "кофе.exe … не отвечает",
        "/chats … зашифровано, не трогаем",
        "rm -rf /schedule … расписания и так не было",
        "обход капчи … котики справились",
        "ping деканат … 100% потерь",
        "/wall … перерисовываем пиксели",
        "шифрование оценок преподавателей … ok",
        "поиск смысла в /library … 0 совпадений",
        "/bookmarks … всё, что отложено на потом",
        "chmod 777 /knt … готово",
        "архив мемов … 4,2 ГБ",
        "колокольчик: 99+ … отключаем",
        "dns knt-mipt.ru → 127.0.0.1",
        "резервная копия … ok",
        "cron: стереть следы в 03:37",
        "ты читаешь это прямо сейчас",
        "стирание следов … ok",
    ]
    if found.get("fragments"):
        done = Find.objects.filter(user=request.user).count()
        lines.insert(12, f"сверка ключа … {done}/{found['fragments']}")
    # Подсказки — вразбивку по всему кругу, а не подряд и не в начале.
    hints = found.get("log", [])
    for n, text in enumerate(hints, 1):
        lines.insert(round(len(lines) * n / (len(hints) + 1)), text)
    return {"lines": lines}


# Подсказки в разметке — не открытым текстом, иначе «Просмотр кода» выдал бы их минуя места.
# Не шифр (ключ — в glitch.js), а занавеска.
VEIL = b"knt"


@register.filter
def veil(value):
    """Данные для glitch.js: JSON в UTF-8, по XOR с VEIL, в base64."""
    data = json.dumps(value, ensure_ascii=False).encode()
    return base64.b64encode(bytes(byte ^ VEIL[i % len(VEIL)] for i, byte in enumerate(data))).decode()


@register.simple_tag
def glitch_clues():
    """Подсказки для порчи карточек, консоли браузера и окна отказа — в данные страницы."""
    found = clues()
    return {place: found.get(place, []) for place in ("card", "devtools", "denied")}


@register.simple_tag
def notfound_clue():
    """Строка для страницы 404 в скине. Легли база или кэш — строки просто нет."""
    try:
        return (clues().get("notfound") or [""])[0]
    except Exception:  # noqa: BLE001 — база, Redis: что именно, зависит от того, что легло
        logger.exception("Подсказка для 404 не загрузилась")
        return ""
