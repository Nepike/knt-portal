"""Какой раздел сайта открыт сейчас — для подсветки пункта в левом меню.

Меню отвечает на вопрос «где я», а не «на какой ровно странице»: уйдя со списка
материалов в конкретный материал, человек из раздела не вышел. Поэтому подсвечиваем
весь раздел, включая карточки и формы.

Имена урлов, а не пути: путь можно поменять, а имя живёт в шаблонах и в `reverse`,
и разойтись они не могут.

Здесь только то, что есть в меню. Страница вне списков (профиль, поддержка) просто
не подсвечивает ничего — это верно, пункта для неё в меню и нет.
"""

SECTIONS = {
    "chats": {"chat_list", "chat_detail"},
    "materials": {"material_list", "material_detail", "material_new", "material_edit"},
    "library": {"book_list", "book_detail", "book_new", "book_edit"},
    "lectorium": {"playlist_list", "playlist_detail", "playlist_new", "playlist_edit"},
    "teachers": {"teacher_list", "teacher_detail"},
    # Профиль сюда НЕ идёт: в него приходят отовсюду — из чата, из ленты отзывов, — и
    # подсвеченные «Студенты» соврали бы про то, откуда человек пришёл.
    "students": {"student_list"},
    "wall": {"wall"},
    "shop": {"shop", "item_card"},
    "bookmarks": {"bookmark_list"},
    "moderation": {"review_queue"},
}

# Имя урла → раздел. Разворачиваем один раз при импорте: на каждый запрос это иначе
# обход всех разделов.
_BY_URL = {name: section for section, names in SECTIONS.items() for name in names}


def section(request):
    """Имя раздела для этого запроса или пустая строка."""
    match = getattr(request, "resolver_match", None)
    return _BY_URL.get(match.url_name, "") if match else ""
