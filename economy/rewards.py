"""За что и сколько дают токенов.

Не сторож и не опрос: **начисление — чистая функция от состояния.** `earned(user)`
перечисляет, за какие ИМЕННО вещи человеку положены токены, журнал помнит, сколько
за каждую уже выплачено, а `sync(user)` дописывает разницу.

«Именно» — ключевое. Считай мы суммой по причине («материалов пять, значит 250»),
удаливший свой материал не получил бы за новый: число вернулось бы к прежнему, а
с ним и «положено». Поэтому у каждой награды свой ключ (`BalanceLog.key`).

Отсюда: повторный вызов ничего не меняет, звать можно откуда угодно; вниз пересчёта нет —
снятый лайк, вещь, ушедшая на повторную проверку или удалённая модератором, выплаченного
не отнимают (иначе «снять и поставить заново» стало бы фермой, а чужое действие — штрафом).
Достижения лягут сюда же предикатами на том же состоянии. А чего в состоянии нет, того
и не начисляем: «зашёл сегодня» тут не появится, пока где-то не будет храниться, за
сколько дней уже заплачено.

Назад забирается одно: награда за вещь, которую автор удалил САМ (`remove`). Без этого
«написал — получил — удалил — написал заново» было фермой: ключ у новой вещи новый.
"""

from collections import namedtuple

from django.db import transaction
from django.db.models import Count, Q, Sum

from .models import BalanceLog
from .services import credit, lock, reclaim

# --- расценки, одно место на весь сайт ---

# Стартовые. Цифра не с потолка: 127 человек из 324 не залили ни файла и не написали
# ни отзыва — без стартовых магазин был бы для них закрыт в день открытия.
WELCOME = 500
MATERIAL = 50  # одобренный материал — основной вклад, и он проходит через проверку
BOOK = 30  # книга реже и проще материала
# Курс лекций — работа другого порядка: снять пару, дотащить гигабайты до сайта и дождаться
# выпечки. Отсюда и цифра, назначенная пользователем: десять материалов.
#
# Платим за КУРС, а не за запись в нём: так решено, и так проще людям — но это значит,
# что двадцать курсов по одной лекции принесут вдесятеро больше, чем один из двадцати.
# Держится на том, что курсы заводят по отдельному праву и каждый проходит проверку;
# начнут дробить — считать придётся по записям.
PLAYLIST = 500
REVIEW_TEXT = 20  # отзыв, в котором есть что читать: таких на весь сайт 183
REVIEW_SCORES = 5  # голые оценки — это клик, но статистике преподавателя они нужны
MODERATION = 5  # за разобранную чужую работу, одобрил её модератор или вернул

# Лайки — единственный признак КАЧЕСТВА, а не объёма. Считаем только чужие (свой голос
# автору ничего не приносит), чистыми (минус дизлайки) и с потолком на запись, иначе
# десяток друзей превращает один отзыв в главный доход.
LIKE = 5
LIKE_CAP = 15
# Платим, когда чистых лайков набралось столько, — сразу за все, дальше за каждый. До
# порога ничего: иначе двое друзей лайкали бы друг другу комментарии без конца, а
# комментариев можно написать сколько угодно.
LIKE_FROM = 5

# Скачивание считается раз в сутки с одного адреса (attachments.views.download), но кто
# именно скачал, нигде не пишется. Отсюда щадящий курс и потолок на файл: накрутка не
# окупается, а честной раздаче хватает (потолок перебирают 27 файлов из 2422).
DOWNLOADS_PER_COIN = 5
DOWNLOAD_CAP = 50
# Через сколько скачиваний звать пересчёт: раздача файлов — самый горячий путь на сайте.
# Недоплаченный остаток подберёт любой следующий пересчёт этого человека.
DOWNLOAD_SYNC_EVERY = 50

# Платим полусотнями — и за Стену, и за скачивания. Иначе лента операций превращается
# в столбик «+1 пиксель» и «+1 скачивают «Программа.pdf»», за которым не видно
# остального: на боевых данных это 20562 строки, у худшего кошелька 2252. С порцией
# в 50 их 411 и 45 соответственно. Остаток не теряется, он ждёт следующего порога.
#
# Ключ у такой награды — НОМЕР ПОРЦИИ («1», «2», …), а не пустая строка. Разница видна
# в журнале: с общим ключом каждый пересчёт дописывал бы разницу одной строкой, и за
# редкий вход она вышла бы на +150 или +400; с номерами каждая полусотня — своя строка
# ровно в 50, и порядок их появления не зависит от того, как часто человек заходит.
WALL_BATCH = 50
DOWNLOAD_BATCH = 50

# Одна причитающаяся награда: за что (reason+key), сколько всего и как подписать в журнале.
Award = namedtuple("Award", "reason key amount note")

R = BalanceLog.Reason


def earned(user):
    """Всё, что человеку положено по нынешнему состоянию базы, по одной строке на вещь.

    Порядок не безразличен: при разовом пересчёте всё ляжет в журнал одной пачкой, и
    сверху в ленте окажется последнее. Поэтому мелочь идёт первой, а материалы — последними.
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
    """Одобренные материалы, книги и курсы лекций. Неодобренные не в счёт: работа
    на проверке ещё может и не выйти, а заплатить за неё значило бы платить за попытку."""
    from lectorium.models import Playlist
    from library.models import Book
    from materials.models import Material

    for model, reason, rate in ((Material, R.MATERIAL, MATERIAL), (Book, R.BOOK, BOOK),
                                (Playlist, R.PLAYLIST, PLAYLIST)):
        rows = model.objects.filter(uploader=user, status=model.Status.APPROVED).values_list("pk", "title")
        for pk, title in rows:
            yield Award(reason, str(pk), rate, title)


def _reviews(user):
    """Отзывы. Дороже тот, в котором есть что посмотреть, причём картинка считается
    содержанием наравне с текстом — ровно как в Review.is_detailed().

    Лайки идут отдельной наградой, но тем же запросом.
    """
    rows = _votes(user.teacher_reviews, user).values_list(
        "pk", "text", "image", "teacher__surname", "likes", "dislikes",
    )

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
    материалом выросла бы ферма «спасибо». Платят за него только чужие лайки.

    Название берём у того владельца, который есть: комментарий висит либо под материалом,
    либо под лекцией. Ключ награды (`c<номер>`) от переезда модели не поменялся — иначе
    уже выплаченное начислилось бы второй раз.
    """
    rows = _votes(user.comments, user).values_list(
        "pk", "material__title", "lecture__title", "likes", "dislikes",
    )

    for pk, material, lecture, likes, dislikes in rows:
        if net := _net(likes, dislikes):
            yield Award(R.LIKES, f"c{pk}", LIKE * net, f"лайки на комментарии к «{material or lecture}»")


def _votes(rows, author):
    """Голоса за записи автора без его собственных: поставить лайк себе можно, но
    награды за него нет — иначе её давал бы каждый свой комментарий."""
    return rows.annotate(
        likes=Count("liked_users", distinct=True)
        - Count("liked_users", filter=Q(liked_users=author), distinct=True),
        dislikes=Count("disliked_users", distinct=True)
        - Count("disliked_users", filter=Q(disliked_users=author), distinct=True),
    )


def _net(likes, dislikes):
    """Оплачиваемые лайки: чистые, с потолком и от порога. Ниже порога — ноль, а не
    долг: спорную запись не награждают, но и не наказывают — иначе первый дизлайк
    отбивал бы охоту писать."""
    net = min(likes - dislikes, LIKE_CAP)
    return net if net >= LIKE_FROM else 0


def _downloads(user):
    """Скачивания всех файлов человека, порциями по DOWNLOAD_BATCH — по награде на порцию.

    Потолок остался на каждый файл отдельно (иначе один популярный файл выбирал бы весь
    лимит владельца), но применяется внутри суммы, а не рождает награду на файл. Так было
    сначала — и в журнал шёл столбик «+1 скачивают «Программа.pdf»» на каждые 5 скачиваний
    КАЖДОГО файла: 20562 строки на боевых данных. Денег это не меняет ни на токен.

    Чего лишились: из журнала больше не видно, какой файл качают. Ему там и не место —
    `File.downloads` держит это живьём и точно, а журнал про деньги.
    """
    from attachments.models import File
    from core.models import Moderated

    # Только то, что прошло проверку: вложение чата и файл черновика видит один автор,
    # и скачивал бы их он же.
    published = Q(material__status=Moderated.Status.APPROVED) | Q(book__status=Moderated.Status.APPROVED)
    rows = File.objects.filter(
        published, uploader=user, downloads__gte=DOWNLOADS_PER_COIN,
    ).values_list("downloads", flat=True)
    total = sum(min(count // DOWNLOADS_PER_COIN, DOWNLOAD_CAP) for count in rows)
    yield from _batches(R.DOWNLOAD, total, DOWNLOAD_BATCH, "скачивают твои файлы")


def _batches(reason, total, size, note):
    """Награда, которая копится и выплачивается порциями, — по строке на порцию.

    Номер порции и есть ключ. Иначе (с одним общим ключом на всю награду) в журнал шла
    бы РАЗНИЦА с прошлого пересчёта: зашёл через месяц — одна строка на +400, зашёл
    сегодня — на +50. Здесь же каждая полусотня своя, и уже записанная строка никогда
    не меняется, сколько бы ни набежало сверху.

    Счётчики только растут, поэтому и номера только прибавляются; уменьшись они (файл
    удалили), выплаченное не отбирается — за этим следит `pending`. Но и заново не
    платится: пока сумма не перерастёт уже оплаченные номера, новых порций нет. Поэтому
    `remove` эти награды не трогает — «удалить и залить заново» тут ничего не даёт.
    """
    for number in range(1, total // size + 1):
        yield Award(reason, str(number), size, note)


def _wall(user):
    """Считаем по WallProfile.painted, а не по журналу доски: туда пишут и заливки
    модератора, и консоль, а награда полагается только за мазок, оплаченный зарядом.

    Порциями и по строке на порцию — как скачивания, см. `_batches`.
    """
    profile = getattr(user, "wall", None)
    if profile:
        yield from _batches(R.WALL, profile.painted, WALL_BATCH, "клетки на Стене")


def _moderated(user):
    """Чужие работы, по которым человек принял решение. Свои не в счёт — иначе модератор
    получал бы дважды: и как автор, и как проверяющий."""
    from lectorium.models import Playlist
    from library.models import Book
    from materials.models import Material

    for model, mark in ((Material, "m"), (Book, "b"), (Playlist, "p")):
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


def pending(user):
    """Что человеку недоплачено: пары (награда, сколько дописать)."""
    paid = _paid(user)
    out = []
    for award in earned(user):
        gap = award.amount - paid.get((award.reason, award.key), 0)
        if gap > 0:
            out.append((award, gap))
    return out


@transaction.atomic
def sync(user):
    """Дописать недостающее. Возвращает, сколько начислено по каждой причине.

    Звать после любого события, меняющего вклад. Лишний вызов безвреден: если разницы
    нет, в журнал не попадёт ни строки.

    Кошелёк занимаем сразу, до чтения журнала: два одновременных вызова (голос и
    скачивание в одну секунду) иначе оба увидели бы «не выплачено» и заплатили дважды.
    """
    if not user or not user.is_authenticated:
        return {}

    lock(user)
    added = {}
    for award, gap in pending(user):
        credit(user, gap, award.reason, note=award.note, key=award.key)
        added[award.reason] = added.get(award.reason, 0) + gap
    return added


def _own(item):
    """Чья это вещь: у отзыва и комментария автор, у материала, книги и курса — загрузивший."""
    return getattr(item, "author_id", None) or getattr(item, "uploader_id", None)


def _branch(comment):
    """Номера комментариев ветки, с ним самим: ответы уезжают каскадом, и за свои среди
    них автору тоже платили."""
    found, level = [comment.pk], [comment.pk]
    while level:
        level = list(type(comment).objects.filter(parent_id__in=level).values_list("pk", flat=True))
        found += level
    return found


def _claims(item):
    """По каким ключам за эту вещь платили и как подписать возврат."""
    kind = item._meta.model_name
    if kind == "review":
        return [(R.REVIEW, str(item.pk)), (R.LIKES, f"r{item.pk}")], f"удалён отзыв о {item.teacher.surname}"
    if kind == "comment":
        return [(R.LIKES, f"c{pk}") for pk in _branch(item)], f"удалён комментарий к «{item.owner.title}»"
    reason, note = {
        "material": (R.MATERIAL, "удалён материал"),
        "book": (R.BOOK, "удалена книга"),
        "playlist": (R.PLAYLIST, "удалён курс"),
    }[kind]
    return [(reason, str(item.pk))], f"{note} «{item.title}»"


def _paid_for(item):
    """Что выплачено за вещь её автору и ещё не забрано: ([(причина, ключ, сумма)], подпись).

    Считаем по журналу, а не по расценкам: ровно то, что по этим ключам начислено,
    минус уже забранное, — повторный возврат поэтому ничего не находит.
    """
    keys, note = _claims(item)
    wanted = Q()
    for reason, key in keys:
        wanted |= Q(reason=reason, key=key)
    rows = (
        BalanceLog.objects.filter(wanted, wallet__user_id=_own(item))
        .values("reason", "key").annotate(total=Sum("amount"))
    )
    return [(row["reason"], row["key"], row["total"]) for row in rows if row["total"] > 0], note


def at_stake(item, by):
    """Сколько токенов спишется, если `by` удалит эту вещь, — для предупреждения перед
    удалением. У не-автора ноль: см. `remove`."""
    if _own(item) != by.pk:
        return 0
    return sum(total for _, _, total in _paid_for(item)[0])


@transaction.atomic
def remove(item, by):
    """Удалить вещь и забрать выплаченное за неё, если удаляет сам автор. Возвращает,
    сколько забрали.

    Правило узкое намеренно. Чужое действие — модератор удалил, сняли лайк, вещь ушла
    на повторную проверку — человеку ничего не стоит: штрафовать за него не за что.
    А своё удаление без возврата было фермой: новая вещь получает новый ключ и
    оплачивается заново. Баланс при этом может уйти в минус (services.reclaim) — иначе
    хватало бы потратить награду до удаления.

    Одним действием с самим удалением, потому что порядок и неделимость обязательны:
    после удаления у вещи нет номера, по которому искать выплаты, а возврат без удаления
    оставил бы человека и без токенов, и с вещью, за которую заплатят заново.
    """
    taken = 0
    if _own(item) == by.pk:
        lock(by)  # до чтения журнала: два удаления разом иначе забрали бы одно и то же дважды
        paid, note = _paid_for(item)
        for reason, key, total in paid:
            reclaim(by, total, reason, note=note, key=key)
            taken += total
    item.delete()
    return taken


def taken_note(taken):
    """Хвост сообщения об удалении. О списании человек должен узнать от нас и сразу,
    а не из журнала кошелька когда-нибудь потом."""
    if not taken:
        return ""
    from core.templatetags.text_extras import plural

    return f" Списано {taken} {plural(taken, 'токен,токена,токенов')} — награда за удалённое."
