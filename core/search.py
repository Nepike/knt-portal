from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Lower, Replace

# Больше слов, чем в ФИО, не бывает; каждое — LIKE по каждому полю.
MAX_WORDS = 4


def _plain(word):
    return word.lower().replace("ё", "е")


def by_name(qs, query, fields=("surname", "name")):
    """Отбор и порядок по строке поиска имени — общий на сайт.

    Каждое слово должно найтись хоть в одном поле, в любом порядке. Сначала те, у кого
    слова в начале поля: список обрезан десятком. Отчество по умолчанию не ищем: на
    «Максим» шла бы Екатерина Максимовна; где оно на виду, его передают в fields.
    """
    words = [_plain(word) for word in query.split()[:MAX_WORDS]]
    if not words:
        return qs

    # Сравниваем приведённое: нижний регистр и ё→е («Пётр» и «Петр»).
    plain = {f"plain_{field}": Replace(Lower(field), Value("ё"), Value("е")) for field in fields}
    qs = qs.annotate(**plain)

    rank = Value(0, output_field=IntegerField())
    for word in words:
        anywhere = Q()
        at_start = Q()
        for alias in plain:
            anywhere |= Q(**{f"{alias}__contains": word})
            at_start |= Q(**{f"{alias}__startswith": word})
        qs = qs.filter(anywhere)
        rank = rank + Case(When(at_start, then=0), default=1, output_field=IntegerField())

    return qs.annotate(name_rank=rank).order_by("name_rank", *fields)
