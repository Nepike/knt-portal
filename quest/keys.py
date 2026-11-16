"""Ключ квеста в консоли отсчёта: каталог key/ и команда found. О поиске сайт прямо не говорит —
только по-хакерски («ключ повреждён», key/ в `ls`, битые .frag). Каждая функция возвращает
ответ консоли: {"text": …, "laugh": …, "warn": …}.
"""

import re

from django.db import transaction

from core.context_processors import zero
from core.throttle import exhausted, strike
from cosmetics.models import UserItem
from economy import rewards
from telegram.notify import SUPPORT, notify

from .models import Broadcast, Find, Fragment, Unlock, bare

# Неверных копий за час на человека: фрагменты короткие, без предела их подобрали бы.
WRONG_PER_HOUR = 20
# Неверных ключей за час: перебором не взять, но смех на каждую попытку — запрос к базе.
WRONG_KEYS_PER_HOUR = 10
# О скольких первых вскрывших ключ сказать в телеграм поддержки.
FIRST_FINDERS = 5
REDACTED = "░░░░"
FILE = re.compile(r"^(?:\./)?key/(\d+)\.frag$")


def slots(user):
    """Ячейки ключа для экрана отсчёта: найденное — значением, прочее — закрыто."""
    found = set(Find.objects.filter(user=user).values_list("fragment_id", flat=True)) if user.is_authenticated else set()
    return [{"number": f.number, "value": f.value if f.pk in found else None} for f in Fragment.objects.all()]


def progress(user):
    """(сверено, всего) — для журнала и ответов found."""
    return Find.objects.filter(user=user).count(), Fragment.objects.count()


def answer(request, command):
    """Ответ на команду про ключ или None, если команда не про него."""
    if command in ("ls key", "ls key/", "ls ./key", "ls ./key/"):
        return listing()
    if command.startswith("cat ") and FILE.match(command[4:].strip()):
        return peek(request.user, int(FILE.match(command[4:].strip())[1]))
    if command == "found" or command.startswith("found "):
        return found(request.user, command[6:].strip())
    if command == "unlock" or command.startswith("unlock "):
        return unlock(request.user, command[7:].strip())
    return None


def listing():
    numbers = list(Fragment.objects.values_list("number", flat=True))
    if not numbers:
        return {"text": "ls: key: нет такого каталога"}
    return {"text": "  ".join(f"{n}.frag" for n in numbers)}


def peek(user, number):
    fragment = Fragment.objects.filter(number=number).first()
    if fragment is None:
        return {"text": f"cat: key/{number}.frag: нет такого файла"}
    if user.is_authenticated and Find.objects.filter(user=user, fragment=fragment).exists():
        return {"text": f"{number}.frag: {fragment.value}"}
    return {"text": f"{number}.frag: {REDACTED} данные повреждены. сверить копию: found <копия>"}


def found(user, copy):
    if not user.is_authenticated:
        return {"text": "found: сверка только для своих. войдите.", "warn": True}
    if not copy:
        done, total = progress(user)
        mine = Find.objects.filter(user=user).select_related("fragment").order_by("fragment__number")
        listed = "  ".join(f"[{f.fragment.number}] {f.fragment.value}" for f in mine)
        return {"text": f"сверено {done} из {total}." + (f"  {listed}" if listed else "")}
    key = f"quest:wrong:{user.pk}"
    if exhausted(key, WRONG_PER_HOUR):
        return {"text": "found: слишком много битых копий. канал закрыт на час.", "warn": True}
    fragment = match(copy)
    if fragment is None:
        strike(key)
        return {"text": "копия не подходит.", "laugh": True}
    _, new = Find.objects.get_or_create(user=user, fragment=fragment)
    done, total = progress(user)
    # Ячейку и счёт страница перерисует сама — строку ключа на экране отсчёта и журнал.
    restored = {"slot": {"number": fragment.number, "value": fragment.value}, "done": done, "total": total}
    if new:
        return {"text": f"копия верна. {fragment.number}.frag восстановлен. сверено {done} из {total}.", **restored}
    return {"text": f"{fragment.number}.frag уже восстановлен. сверено {done} из {total}.", **restored}


def key():
    """Ключ целиком — значения фрагментов по номерам, слитно, той же чисткой, что введённый."""
    return squash("".join(Fragment.objects.values_list("value", flat=True)))


def squash(attempt):
    """Введённый ключ без регистра, пробелов и разделителей: «7f3a-b0b0 …» = «7f3ab0b0…»."""
    return re.sub(r"[\s\-_,.:;|/]", "", attempt.lower())


def unlock(user, attempt):
    """Ввод ключа — только в красный день, после нуля. Верный — награда и рамка, раз на человека."""
    if not zero():
        return {"text": "рано. время ещё не вышло."}
    if not user.is_authenticated:
        return {"text": "ключ принимаем только у своих — войдите на сайт.", "warn": True}
    if Unlock.objects.filter(user=user).exists():
        return {"text": "ключ уже принят. сайт вернётся 2 декабря."}
    if not attempt:
        return {"text": "unlock <ключ>"}
    strikes = f"quest:unlock:{user.pk}"
    if exhausted(strikes, WRONG_KEYS_PER_HOUR):
        return {"text": "unlock: слишком много попыток. канал закрыт на час.", "warn": True}
    whole = key()
    if not whole or squash(attempt) != whole:
        strike(strikes)
        return {"text": "ключ не подошёл.", "laugh": True}
    frame = reward(user)
    prize = f"{rewards.QUEST_KEY} токенов" + (f" и рамка «{frame.name}»" if frame else "")
    return {"text": f"ключ принят. сайт вернётся 2 декабря. тебе — {prize}.", "unlocked": True}


@transaction.atomic
def reward(user):
    """Отметить вскрытый ключ, начислить токены, выдать рамку → рамка или None. О первых
    FIRST_FINDERS — в чат поддержки, после коммита."""
    _, new = Unlock.objects.get_or_create(user=user)
    rewards.sync(user)
    broadcast = Broadcast.current()
    frame = broadcast.frame if broadcast else None
    if frame:
        UserItem.objects.get_or_create(user=user, item=frame)
    place = Unlock.objects.count()
    if new and place <= FIRST_FINDERS:
        transaction.on_commit(lambda: notify(SUPPORT, "quest/telegram_unlocked.html", {"person": user, "place": place}))
    return frame


def match(copy):
    """Фрагмент по копии: целиком или хвост после «=», «:», «]» — подходит и «frag[3/7]=7f3a»."""
    tries = {bare(copy), bare(re.split(r"[=:\]]", copy)[-1])} - {""}
    for fragment in Fragment.objects.all():
        if bare(fragment.value) in tries:
            return fragment
    return None
