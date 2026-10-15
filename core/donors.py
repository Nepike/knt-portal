"""Выгрузка донатов Boosty → подписи для страницы «О сайте».

Без Django: на входе байты файла, на выходе строки. Из файла берутся только имена —
почты и суммы в базу не попадают вовсе.
"""

import csv
import io
from collections import defaultdict


def fold(name):
    """Ключ для сравнения и сортировки: регистр, «ё» и лишние пробелы не важны."""
    return " ".join(name.split()).casefold().replace("ё", "е")


def read(raw):
    """Имена из выгрузки (stat-donations.csv): только донаты, как в файле, с повторами."""
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";")
    if "name" not in (rows.fieldnames or []):
        raise ValueError("в файле нет столбца name — это не выгрузка донатов Boosty")
    return [row["name"] for row in rows if row.get("type") == "donation" and row["name"].strip()]


def signatures(names, skip=()):
    """Подписи для страницы: каждый человек один раз, без пропущенных, по алфавиту.

    Фамилия сокращается до буквы — страницу видит весь интернет. Двоим разным, кто
    сократился одинаково, буквы прибавляются, пока подписи не разойдутся («Елена Ме.»,
    «Елена Ми.»). Ник из одного слова остаётся как есть.
    """
    skipped = {fold(name) for name in skip}
    people = {}
    for name in names:
        key = fold(name)
        if key and key not in skipped:
            people.setdefault(key, " ".join(name.split()))
    return sorted(_shorten(list(people.values())), key=fold)


def _shorten(people):
    short = {person: person for person in people if len(person.split()) == 1}
    pending = [person for person in people if person not in short]
    letters = 1
    while pending:
        groups = defaultdict(list)
        for person in pending:
            words = person.split()
            groups[words[0], words[-1][:letters]].append(person)
        pending = []
        for (first, part), group in groups.items():
            if len(group) > 1 and all(len(person.split()[-1]) > letters for person in group):
                pending += group
                continue
            # Один в группе — сокращаем; разошлись только полной фамилией — пишем целиком.
            for person in group:
                whole = len(group) > 1 or part == person.split()[-1]
                short[person] = person if whole else f"{first} {part}."
        letters += 1
    return [short[person] for person in people]
