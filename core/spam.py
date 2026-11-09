"""Штрафные баллы обращению гостя в поддержку или контакты.

Каждый подозрительный признак добавляет баллы; набрал THRESHOLD — в чат не уходит.
Признаки и веса подобраны по 820 обращениям старого сайта, где спам размечал Akismet,
а ошибки поправлены руками (ноябрь 2026): при пороге 9 схема отсеивала 86% старого
спама и ни одного из 56 живых, самый подозрительный живой набирал 6. Байес на тех же
данных ловил больше, но задевал каждое седьмое живое письмо — живых примеров всего 56,
а для весов тысяч слов этого мало. Здесь признаков дюжина, и каждый разобран головой.

Словарь ловит только то, чего студент или абитуриент не напишет: «продвинутый курс»,
«посещаемость лекций», «криптография», «ваш сайт не работает» и spin электрона
сюда не попадают — каждое такое слово проверено на живых обращениях.

Без Django: модуль чистый, его гоняет и скрипт сверки со старой базой.
"""

import re

THRESHOLD = 9
FAST = 3  # секунд; быстрее человек не напишет ни вопроса, ни имени, ни контакта

MARKUP = re.compile(
    r"<\s*/?\s*(a|b|u|i|s|h\d|ul|li|strong|em|p|br|div|span|img|font)\b[^>]*>|\[/?(url|link|img|b|u)\b|&#\d{3,4};",
    re.I,
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
TLD = (
    "com|net|org|info|biz|ru|su|рф|io|cc|co|me|top|online|site|pro|xyz|link|click|shop|store|live|app|dev|ly"
    "|to|gg|ai|de|eu|uk|us|ua|kz|by|host|space|fun|club|vip|win|bet|casino|pw|lat|gd|tk|ml|in|pl|cn|sm"
)
# Ссылку узнаём и без https://: «psee.io/8rkbf9» телеграм сам делает ссылкой
LINK = re.compile(r"(?:https?://|www\.)[^\s<>\"']+|\b(?:[\w-]+\.)+(?:" + TLD + r")\b(?:/[^\s<>\"']*)?", re.I)
# Свои и те, на кого ссылается живой человек: сайт, институт, ВК, телеграм, олимпиады
OURS = re.compile(
    r"^(?:[\w-]+\.)*(knt-mipt\.ru|inbicst\.ru|mipt\.ru|phystech\.edu|vk\.com|vk\.ru|t\.me|telegram\.me"
    r"|gosuslugi\.ru|olimpiada\.ru)$",
    re.I,
)
SHORTENERS = re.compile(
    r"^(bit\.ly|is\.gd|tinyurl\.com|psee\.io|psce\.pw|goo\.su|clck\.ru|cutt\.ly|ur3\.us|5\.gp|t\.co)$", re.I,
)
CYRILLIC = re.compile(r"[а-яё]", re.I)
WORD = re.compile(r"[a-zа-яё]+", re.I)
PHONE = re.compile(r"(?:\+|\b)[78]?[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b|\+\d{10,13}\b")

# Группа слов срабатывает целиком: десять казиношных слов — те же баллы, что одно. Вес 8 —
# чтобы одна группа с почтой для ответа уже не проходила: среди живых словарь не сработал ни разу
LEXICON = {name: re.compile(pattern, re.I) for name, pattern in {
    "реклама и SEO": (
        r"\bseo\b|\bсео\b|продвижени\w* (сайт|бизнес|в поиск|товар|услуг)|продвинем|посещаемост\w* (ваш|сайт)"
        r"|контекстн\w* реклам|я(ндекс)?[ .]?директ|маркетплейс|авито|\bлид(ов|ы)\b|backlink|ranking|webmaster"
        r"|subscribers|youtube growth|promote your|grow (your|tg)"
    ),
    "налоги и чеки": (
        r"\bндс\b|сч[её]т\w*-фактур|\bупд\b|кассов\w* чек|чеки (для|под) отч[её]т|налогов\w* (оптимиз|нагрузк)"
        r"|оптимизац\w* налог|\bифнс\b|юридическ\w* адрес"
    ),
    "базы и рассылки": (
        r"баз(а|ы|у) (данных|клиент|поставщ|потенциальн|директор)|потенциальн\w* (клиент|покупател|заказчик)"
        r"|рассылк\w* (сообщ|реклам)|сервис\w* рассыл|отправк\w* (вашего )?реклам|лиц,? принимающ"
    ),
    "казино и ставки": (
        r"casino|казино|jackpot|slot machine|mega slots|spin to win|start spinning|(one|next|last|first) spin"
        r"|free ?spins|фриспин|betting|sportsbet|букмекер|ставк\w* на спорт|1win|1вин|mostbet|мостбет|pin-?up"
        r"|888starz|раменбет|cash vault|bonus code|promo code"
    ),
    "крипта и даркнет": (
        r"криптовалют|криптой|\bcrypto\b|cryptocurrency|bitcoin|биткоин|\busdt\b|binance|кракен|kraken"
        r"|даркнет|darknet|seed phrase|наследств|inheritance"
    ),
    "для взрослых": (
        r"porn|\bsex|fuck|hot girl|horny|escort|эскорт|\bdating\b|интим|cialis|сиалис|viagra|виагр"
        r"|без рецепт|rezeptfrei|without prescription"
    ),
    "предложение бизнесу": (
        r"ваш(его|ему|им|ем)? (бизнес|бренд|веб-ресурс)|ваш\w* продукц|ваших (клиент|покупател)"
        r"|your business|your products?|your company|your prices?|price ?list|send (us|me) your offer"
        r"|узнать вашу цену|cooperation|partnership|коммерческ\w* предложен|сотрудничеств\w* на постоян"
    ),
    "займы и заработок": r"микрозайм|займ\w* (на карт|онлайн)|\bloan|passive income|пассивн\w* доход|earn(ing)? money|money-making",
    # «Извините за спам, пишу третий раз» — так пишет и живой, поэтому только обороты рассыльщиков
    "признаётся в рассылке": r"попали в рассылку|рассылк\w* без вашего согласия|unsubscribe here|opt out here",
}.items()}


def _host(link):
    bare = re.sub(r"^(?:https?://)", "", link, flags=re.I)
    return re.split(r"[/?#:]", bare, maxsplit=1)[0].lower().rstrip(".,)")


def _switches(word):
    """Сколько раз слово переходит с кириллицы на латиницу и обратно.
    «Здравcтвyйте» с латинскими c и y — четыре; «PDFке» — один, так пишут живые."""
    scripts = ["c" if CYRILLIC.match(ch) else "l" for ch in word]
    return sum(1 for a, b in zip(scripts, scripts[1:]) if a != b)


def _name(name):
    name = (name or "").strip()
    if re.search(r"\d", name) or re.search(r"slife|xrumer", name, re.I):
        return "имя с цифрами или меткой рассыльщика"
    # «JosephWeism», «RobertoGeorm». Одно имя латиницей (Alexander, Markus) — не признак:
    # так пишет и иностранный абитуриент.
    if re.fullmatch(r"[A-Z][a-z]+[A-Z][a-z]+", name):
        return "имя — два слова слитно латиницей"
    return ""


def _contact(contact):
    """(признак, баллы) по строке для ответа. Телеграм и ВК оставляют живые: из 735 старых
    спамов так написал один, из 56 живых — 27."""
    contact = (contact or "").strip()
    if EMAIL.fullmatch(contact):
        local = contact.split("@")[0]
        if local.count(".") >= 3 or re.search(r"\d{5,}", local):
            return "почта-мусор для ответа", 4
        return "почта для ответа", 1
    if re.search(r"(^|\s|:)@\w{4,}|t\.me/|vk\.com/|\b(тг|телеграм\w*|telegram|tg|вк|vk)\b", contact, re.I):
        return "телеграм или ВК для ответа", -5
    if re.fullmatch(r"[A-Za-z]{1,2}|[A-Za-z]+(?: [A-Za-z]+)+", contact):
        return "набор латинских букв вместо контакта", 4
    return "", 0


def score(text, name="", contact="", seconds=None):
    """(баллы, [(признак, баллы), …]).

    `seconds` — сколько прошло от открытия формы до отправки; None — метки нет
    или она поддельная: значит, поля прислали не с нашей страницы.
    """
    text = text or ""
    hits = []
    if MARKUP.search(text):
        hits.append(("HTML-разметка", 20))
    alien = [host for host in map(_host, (m.group(0) for m in LINK.finditer(EMAIL.sub(" ", text)))) if not OURS.match(host)]
    if alien:
        hits.append(("чужая ссылка" if len(alien) == 1 else f"чужих ссылок: {len(alien)}", min(6 + 3 * (len(alien) - 1), 12)))
    if any(SHORTENERS.match(host) for host in alien):
        hits.append(("сокращённая ссылка", 4))
    if any(_switches(word) >= 2 for word in WORD.findall(text)):
        hits.append(("латинские буквы внутри русских слов", 8))
    if not CYRILLIC.search(text):
        hits.append(("ни одной русской буквы", 4))
    groups = [group for group, pattern in LEXICON.items() if pattern.search(text)]
    if groups:
        hits.append(("словарь: " + ", ".join(groups), min(8 * len(groups), 16)))
    if PHONE.search(text):
        hits.append(("телефон в тексте", 2))
    if sign := _name(name):
        hits.append((sign, 5))
    sign, points = _contact(contact)
    if sign:
        hits.append((sign, points))
    if seconds is None:
        hits.append(("нет метки времени формы", 6))
    elif seconds < FAST:
        hits.append((f"отправлено за {seconds} с", 6))
    return sum(points for _, points in hits), hits
