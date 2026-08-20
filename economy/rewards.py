"""За что и сколько дают токенов.

Устроено не сторожем и не опросом: **начисление — это чистая функция от состояния.**
`earned(user)` перечисляет, за какие ИМЕННО вещи человеку положены токены и сколько
за каждую, журнал операций помнит, сколько за каждую уже выплачено, а `sync(user)`
дописывает разницу.

Ключевое слово тут «именно». Считать суммой по причине («материалов пять, значит 250»)
было бы проще, но тогда удаливший свой материал больше не получал бы за новый: число
материалов вернулось бы к прежнему, а вместе с ним и «положено». Поэтому у каждой
награды свой ключ (`BalanceLog.key`), и сравнение идёт по нему.

Отсюда:

* повторный вызов ничего не меняет — звать можно откуда угодно и сколько угодно,
  а команда `recount_tokens` это тот же `sync`, прогнанный по всем;
* уже выплаченное назад не забирается: удалили материал, сняли лайк — токены остаются.
  Отбирать потраченное нечестно, а «снять и поставить лайк заново» иначе стало бы фермой.
  Привести всё строго к состоянию умеет только `recount_tokens --reset`;
* достижения лягут сюда же: «10 материалов», «первый отзыв» — предикаты на том же
  состоянии, что и суммы ниже.

Чего в состоянии нет, того и не начисляем. Поэтому «зашёл сегодня» тут не появится,
пока где-нибудь не будет храниться, за сколько дней уже заплачено: событие, не
оставившее следа, пересчитать нельзя.
"""

from collections import namedtuple

from django.db.models import Count, Sum

from .models import BalanceLog, Wallet
from .services import credit

# --- расценки, одно место на весь сайт ---

# Стартовые. Цифра не с потолка: 127 человек из 324 не залили ни файла и не написали ни
# отзыва, и другого источника у них нет. Меньше — и магазин в день открытия закрыт для
# большинства, а это не мотивация, а зависть.
WELCOME = 500
MATERIAL = 50  # одобренный материал — основной вклад, и он проходит через проверку
BOOK = 30  # книга реже и проще материала
REVIEW_TEXT = 20  # отзыв, в котором есть что читать: таких на весь сайт 183
REVIEW_SCORES = 5  # голые оценки — это клик, но статистике преподавателя они нужны
MODERATION = 5  # за разобранную чужую работу, одобрил её модератор или вернул

# Лайки — единственный признак КАЧЕСТВА, а не объёма: загрузок можно наделать, а чужой
# палец вверх в одиночку не поставишь. Считаем чистыми (минус дизлайки) и с потолком
# на запись: иначе десяток друзей превращает один отзыв в главный доход.
LIKE = 5
LIKE_CAP = 15

# Скачивания — лучший признак полезности, но счётчик лежит прямо на файле, и кто именно
# скачал, нигде не пишется: «скачал свой файл сто раз» ничем не ловится. Отсюда щадящий
# курс и потолок на файл — накрутка перестаёт окупаться, а честной раздаче потолка хватает
# (сейчас его перебирают 27 файлов из 2422).
DOWNLOADS_PER_COIN = 5
DOWNLOAD_CAP = 50
# Через сколько скачиваний звать пересчёт. Не каждые пять: раздача файлов — самый горячий
# путь на сайте, а лента операций из строк «+1 скачивание» не читается. Недоплаченный
# остаток подберёт любой следующий пересчёт этого человека.
DOWNLOAD_SYNC_EVERY = 50

# Стена: клетка — токен, но платим полусотнями. Иначе на каждый мазок ложилась бы строка
# в журнал, и лента операций превратилась бы в шум, за которым не видно остального.
WALL_BATCH = 50

# Одна причитающаяся награда: за что (reason+key), сколько всего и как подписать в журнале.
Award = namedtuple("Award", "reason key amount note")

R = BalanceLog.Reason


def earned(user):
    """Всё, что человеку положено по нынешнему состоянию базы, по одной строке на вещь.

    Порядок не безразличен: при разовом пересчёте всё ляжет в журнал одной пачкой, и
    сверху в ленте окажется последнее. Поэтому мелочь идёт первой, а материалы и книги —
    последними: человек должен увидеть «+50 за конспект», а не шесть строк «+1 скачали».
    """
    return [
        Award(R.WELCOME, "", WELCOME, "добро пожаловать"),
        *_downloads(user),
        *_wall(user),
        *_comments(user),
        *_moderated(user),
        *_reviews(user),
        *_uploads(user),
    ]


def _uploads(user):
    """Одобренные материалы и книги. Неодобренные не в счёт: работа на проверке ещё
    может и не выйти, а заплатить за неё значило бы платить за попытку."""
    from library.models import Book
    from materials.models import Material

    for model, reason, rate in ((Material, R.MATERIAL, MATERIAL), (Book, R.BOOK, BOOK)):
        rows = model.objects.filter(uploader=user, status=model.Status.APPROVED).values_list("pk", "title")
        for pk, title in rows:
            yield Award(reason, str(pk), rate, title)


def _reviews(user):
    """Отзывы. Дороже тот, в котором есть что посмотреть, — и картинка тут считается
    содержанием наравне с текстом, ровно как в Review.is_detailed(): сайт такой отзыв
    показывает всегда и даёт за него голосовать, значит и платить надо как за полный.

    Лайки идут отдельной наградой, но одним запросом: второй раз ходить за теми же
    строками незачем.
    """
    rows = user.teacher_reviews.annotate(
        likes=Count("liked_users", distinct=True),
        dislikes=Count("disliked_users", distinct=True),
    ).values_list("pk", "text", "image", "teacher__surname", "likes", "dislikes")

    for pk, text, image, surname, likes, dislikes in rows:
        detailed = bool(text or image)
        yield Award(
            R.REVIEW, str(pk), REVIEW_TEXT if detailed else REVIEW_SCORES,
            f"отзыв о {surname}" if detailed else f"оценки {surname}",
        )
        if net := _net(likes, dislikes):
            yield Award(R.LIKES, f"r{pk}", LIKE * net, f"лайки на отзыве о {surname}")


def _comments(user):
    """Сам комментарий не оплачивается — ни с текстом, ни с картинкой: иначе под каждым
    материалом выросла бы ферма «спасибо». Платят за него только чужие лайки."""
    rows = user.material_comments.annotate(
        likes=Count("liked_users", distinct=True),
        dislikes=Count("disliked_users", distinct=True),
    ).values_list("pk", "material__title", "likes", "dislikes")

    for pk, title, likes, dislikes in rows:
        if net := _net(likes, dislikes):
            yield Award(R.LIKES, f"c{pk}", LIKE * net, f"лайки на комментарии к «{title}»")


def _net(likes, dislikes):
    """Чистые лайки, с потолком. Минус уводим в ноль, а не в долг: спорную запись
    не награждают, но и не наказывают — иначе первый дизлайк отбивал бы охоту писать."""
    return min(max(likes - dislikes, 0), LIKE_CAP)


def _downloads(user):
    """Потолок — на каждый файл отдельно, поэтому и награда пофайловая: иначе один
    популярный файл выбирал бы весь лимит владельца."""
    from attachments.models import File

    rows = File.objects.filter(uploader=user, downloads__gte=DOWNLOADS_PER_COIN).values_list("pk", "name", "downloads")
    for pk, name, downloads in rows:
        yield Award(R.DOWNLOAD, str(pk), min(downloads // DOWNLOADS_PER_COIN, DOWNLOAD_CAP), f"скачивают «{name}»")


def _wall(user):
    """Считаем по WallProfile.painted, а не по журналу доски: туда пишут и заливки
    модератора, и консоль, а награда полагается только за мазок, оплаченный зарядом.

    Ключа нет — счётчик и так только растёт, отнимать у него нечего.
    """
    profile = getattr(user, "wall", None)
    if profile and (paid := (profile.painted // WALL_BATCH) * WALL_BATCH):
        yield Award(R.WALL, "", paid, "клетки на Стене")


def _moderated(user):
    """Чужие работы, по которым человек принял решение. Свои не в счёт — иначе модератор
    получал бы дважды: и как автор, и как проверяющий."""
    from library.models import Book
    from materials.models import Material

    for model, mark in ((Material, "m"), (Book, "b")):
        rows = (
            model.objects.filter(reviewed_by=user).exclude(uploader=user).values_list("pk", "title")
        )
        for pk, title in rows:
            yield Award(R.MODERATION, f"{mark}{pk}", MODERATION, f"проверено: {title}")


def _paid(user):
    """Сколько уже начислено за каждую вещь. Только плюсы: трата — не отмена награды."""
    rows = (
        BalanceLog.objects.filter(wallet__user=user, amount__gt=0)
        .values("reason", "key").annotate(total=Sum("amount"))
    )
    return {(row["reason"], row["key"]): row["total"] for row in rows}


def pending(user, fresh=False):
    """Что человеку недоплачено: пары (награда, сколько дописать).

    fresh — считать так, будто журнал пуст. Нужно пробному прогону `recount_tokens
    --reset`: он показывает, что получится ПОСЛЕ сноса, а сносить на пробе нечего.
    """
    paid = {} if fresh else _paid(user)
    out = []
    for award in earned(user):
        gap = award.amount - paid.get((award.reason, award.key), 0)
        if gap > 0:
            out.append((award, gap))
    return out


def sync(user):
    """Дописать недостающее. Возвращает, сколько начислено по каждой причине.

    Звать после любого события, меняющего вклад. Лишний вызов безвреден: если разницы
    нет, в журнал не попадёт ни строки.
    """
    if not user or not user.is_authenticated:
        return {}

    Wallet.objects.get_or_create(user=user)
    added = {}
    for award, gap in pending(user):
        credit(user, gap, award.reason, note=award.note, key=award.key)
        added[award.reason] = added.get(award.reason, 0) + gap
    return added
