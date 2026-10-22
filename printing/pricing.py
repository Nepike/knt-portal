"""Себестоимость печати и разбор выбора страниц.

Без Django, как `intake/spec.py`: чистые функции, и выбор «1-3,7» тем же кодом разбирает
агент на М4 — печатает он ровно те страницы, которые посчитал сайт. Деньги — в копейках.

    тонер на страницу   = цена картриджа / ресурс × (заливка / 5%) × поправка
    барабан на страницу = цена барабана / ресурс
    задание             = копии × (бумага на копию + Σ тонер(заливка) + страниц × барабан)
"""

import re
from dataclasses import asdict, dataclass
from math import ceil

# Ресурс картриджа производитель указывает при заливке 5% (ISO/IEC 19752).
BASE_COVERAGE = 0.05
# Страница темнее этого отмечается студенту: тёмный слайд стоит как пять текстовых.
DARK = 0.25

_RANGE = re.compile(r"(\d+)(?:-(\d+))?")


class BadSelection(ValueError):
    """Выбор страниц, который нельзя напечатать. Текст — для человека."""


def _pages(count):
    if count % 10 == 1 and count % 100 != 11:
        return "страница"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "страницы"
    return "страниц"


def pages_of(selection, total):
    """Номера страниц по строке вида «1-3,7»: по возрастанию и без повторов.

    Пустая строка — отказ, а не «все»: снятые на экране галочки не должны превращаться
    в печать всего документа. Все страницы — это явное «1-N».
    """
    text = re.sub(r"\s*[-–—]\s*", "-", selection.strip())
    chosen = set()
    for part in filter(None, re.split(r"[,;\s]+", text)):
        match = _RANGE.fullmatch(part)
        if not match:
            raise BadSelection(f"Не понял «{part}». Номера и диапазоны пишутся через запятую, например 1-3,7")
        first = int(match[1])
        last = int(match[2] or first)
        first, last = min(first, last), max(first, last)
        # Границы — до того, как разворачивать диапазон: «1-999999999» не должен строить
        # множество на миллиард номеров.
        if first < 1 or last > total:
            wrong = first if first < 1 else last
            raise BadSelection(f"Страницы {wrong} нет: в документе {total} {_pages(total)}")
        chosen.update(range(first, last + 1))
    if not chosen:
        raise BadSelection("Не выбрано ни одной страницы")
    return sorted(chosen)


def selection_of(numbers):
    """Обратно: [1, 2, 3, 7] → «1-3,7». Так выбор хранится в задании и едет агенту."""
    parts, numbers = [], sorted(set(numbers))
    start = previous = None
    for number in numbers:
        if previous is not None and number == previous + 1:
            previous = number
            continue
        if start is not None:
            parts.append(f"{start}-{previous}" if previous > start else str(start))
        start = previous = number
    if start is not None:
        parts.append(f"{start}-{previous}" if previous > start else str(start))
    return ",".join(parts)


def sheets(count, copies=1, duplex=False):
    """Листов бумаги. Каждая копия начинается с нового листа: нечётная страница
    на обороте следующей копии разрезала бы стопку пополам."""
    return copies * (ceil(count / 2) if duplex else count)


def sides(count, copies=1):
    """Сколько уходит в лимит: напечатанные стороны. Двусторонний лист — две."""
    return count * copies


@dataclass(frozen=True)
class Costs:
    paper_sheet: int  # копеек за лист
    toner_price: int  # копеек за картридж
    toner_yield: int  # страниц на картридж при заливке 5%
    drum_price: int  # копеек за барабан
    drum_yield: int  # страниц на барабан
    toner_factor: float = 1.0  # калибровка: во сколько раз принтер ест больше, чем по пикселям

    def toner(self, share):
        return self.toner_price / self.toner_yield * (share / BASE_COVERAGE) * self.toner_factor

    def drum(self):
        return self.drum_price / self.drum_yield

    def as_dict(self):
        return asdict(self)


def cost(shares, copies, duplex, costs):
    """Себестоимость задания в копейках. shares — заливка выбранных страниц, доли 0..1."""
    one = (
        costs.paper_sheet * sheets(len(shares), 1, duplex)
        + sum(costs.toner(share) for share in shares)
        + len(shares) * costs.drum()
    )
    return round(copies * one)


def rubles(kopecks):
    """«1,24 ₽» — так сумма показывается человеку."""
    return f"{kopecks // 100},{kopecks % 100:02d} ₽"
