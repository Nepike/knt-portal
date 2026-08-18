from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Lower, Replace

# Больше слов, чем в полном ФИО, в запросе не бывает — а длинная строка превратилась бы
# в десяток LIKE по каждому полю разом.
MAX_WORDS = 4


def _plain(word):
    return word.lower().replace("ё", "е")


def by_name(qs, query, fields=("surname", "name")):
    """Отбор и порядок для строки поиска по имени. Общий на весь сайт: чаты, преподаватели.

    Строку режем на слова, и каждое слово должно найтись хоть в одном из полей. Раньше
    искали строкой целиком по каждому полю отдельно, и «Максим Щучкин» не находил никого:
    такой строки нет ни в имени, ни в фамилии — она есть только в них вместе. Порядок слов
    при этом любой: «Щучкин Максим» и «Максим Щучкин» — одно и то же.

    Сортировка — сначала те, у кого слова стоят В НАЧАЛЕ поля. Это важнее, чем кажется:
    список обрезан десятком, и по «Иван» первыми должны идти Иван и Иванов, а не десять
    Ивановых, среди которых нужный не поместился.

    Отчество по умолчанию не ищем (fields): по нему находились однофамильцы чужого имени —
    на «Максим» первой шла Екатерина Максимовна. Там, где отчество на виду (преподаватели),
    его можно передать явно — тогда сработает и ФИО целиком.
    """
    words = [_plain(word) for word in query.split()[:MAX_WORDS]]
    if not words:
        return qs

    # Сравниваем не с самим полем, а с приведённым к общему виду: нижний регистр и ё→е.
    # В базе есть и «Пётр», и «Петр», и пишут в поиске обычно без ё — без этого человек
    # не находится по тому написанию, которого не оказалось у него в анкете.
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
