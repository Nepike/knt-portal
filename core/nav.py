"""Раздел сайта для подсветки пункта меню. Подсвечивается весь раздел, вместе с
карточками и формами. Имена урлов, а не пути. Страница вне меню не подсвечивает ничего.
"""

SECTIONS = {
    # Меню у незалогиненного своё: эти два раздела есть только в нём.
    "about": {"about"},
    "contacts": {"contacts"},
    "chats": {"chat_list", "chat_detail"},
    "materials": {"material_list", "material_detail", "material_new", "material_edit"},
    "library": {"book_list", "book_detail", "book_new", "book_edit"},
    "lectorium": {"playlist_list", "playlist_detail", "playlist_new", "playlist_edit"},
    "teachers": {"teacher_list", "teacher_detail"},
    # Профиль не здесь: в него приходят отовсюду.
    "students": {"student_list"},
    "wall": {"wall"},
    "shop": {"shop", "item_card"},
    "printing": {"print_home", "print_job"},
    "bookmarks": {"bookmark_list"},
    "moderation": {"review_queue"},
    "pulse": {"pulse_home", "pulse_feed", "pulse_people", "pulse_person", "pulse_tokens", "pulse_wall", "pulse_system"},
}

# Имя урла → раздел, один раз при импорте.
_BY_URL = {name: section for section, names in SECTIONS.items() for name in names}


def section(request):
    """Имя раздела для этого запроса или пустая строка."""
    match = getattr(request, "resolver_match", None)
    return _BY_URL.get(match.url_name, "") if match else ""
