"""Награды токенами — чистая функция от состояния базы.

`earned(user)` перечисляет положенное по вещам, у каждой награды свой ключ
(`BalanceLog.key`); `sync(user)` дописывает разницу. Повторный вызов ничего не меняет.
Вниз пересчёта нет. Забирается только награда за вещь, которую автор удалил сам
(`remove`).
"""

from collections import namedtuple

from django.db import transaction
from django.db.models import Count, Q, Sum

from .models import BalanceLog
from .services import credit, lock, reclaim

# --- расценки, одно место на весь сайт ---

# Стартовые: без них магазин был бы закрыт для тех, кто ничего не заливал.
WELCOME = 500
MATERIAL = 50  # одобренный материал — основной вклад, и он проходит через проверку
BOOK = 30  # книга реже и проще материала
# За курс, а не за запись: курсы заводят по отдельному праву, и каждый проходит
# проверку. Начнут дробить курсы — считать по записям.
PLAYLIST = 250
REVIEW_TEXT = 20  # отзыв, в котором есть что читать
REVIEW_SCORES = 5  # голые оценки — это клик, но статистике преподавателя они нужны
MODERATION = 5  # за разобранную чужую работу, одобрил её модератор или вернул

# Лайки — признак КАЧЕСТВА. Только чужие, чистые (минус дизлайки) и с потолком на запись.
LIKE = 5
LIKE_CAP = 15
# Платим, когда чистых лайков набралось столько, — сразу за все, дальше за каждый. До порога
# ничего: иначе друзья лайкали бы друг другу комментарии без конца.
LIKE_FROM = 5

# Скачивание считается раз в сутки с адреса (attachments.views.download), кто скачал — не
# пишется. Отсюда щадящий курс и потолок на файл: накрутка не окупается.
DOWNLOADS_PER_COIN = 5
DOWNLOAD_CAP = 50
# Через сколько скачиваний звать пересчёт: раздача — самый горячий путь; остаток доберёт следующий.
DOWNLOAD_SYNC_EVERY = 50

# Стену и скачивания платим полусотнями: иначе журнал зарастает строками «+1». Ключ — НОМЕР
# ПОРЦИИ («1», «2», …): каждая полусотня — своя строка ровно в 50, как часто ни заходи.
WALL_BATCH = 50
DOWNLOAD_BATCH = 50

# Ключ от глитч-взлома (quest): раз на человека, держится на записи quest.Unlock.
QUEST_KEY = 1500

# Одна причитающаяся награда: за что (reason+key), сколько всего и как подписать в журнале.
Award = namedtuple("Award", "reason key amount note")

R = BalanceLog.Reason


def earned(user):
    """Всё, что человеку положено по нынешнему состоянию базы, по одной строке на вещь.
    Мелочь — первой: при разовом пересчёте последнее окажется сверху ленты."""
    return [
        Award(R.WELCOME, "", WELCOME, "добро пожаловать"),
        *_quest(user),
        *_downloads(user),
        *_wall(user),
        *_comments(user),
        *_moderated(user),
        *_reviews(user),
        *_uploads(user),
    ]


def _uploads(user):
    """Одобренные материалы, книги и курсы: неодобренное ещё может и не выйти."""
    from lectorium.models import Playlist
    from library.models import Book
    from materials.models import Material

    for model, reason, rate in ((Material, R.MATERIAL, MATERIAL), (Book, R.BOOK, BOOK),
                                (Playlist, R.PLAYLIST, PLAYLIST)):
        rows = model.objects.filter(uploader=user, status=model.Status.APPROVED).values_list("pk", "title")
        for pk, title in rows:
            yield Award(reason, str(pk), rate, title)


def _reviews(user):
    """Отзывы: дороже тот, где есть что посмотреть — текст или картинка, как в
    Review.is_detailed(). Лайки — отдельной наградой, тем же запросом."""
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
    """Комментарий сам не оплачивается (иначе ферма «спасибо») — только чужие лайки. Ключ
    `c<номер>` от переезда модели не менялся: иначе выплаченное начислилось бы снова."""
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
    """Оплачиваемые лайки: чистые, с потолком и от порога. Ниже порога — ноль, а не долг."""
    net = min(likes - dislikes, LIKE_CAP)
    return net if net >= LIKE_FROM else 0


def _downloads(user):
    """Скачивания всех файлов человека порциями по DOWNLOAD_BATCH. Потолок — на каждый файл,
    но внутри суммы, а не наградой на файл. Какой файл качают, держит `File.downloads`."""
    from attachments.models import File
    from core.models import Moderated

    # Только прошедшее проверку: вложение чата и черновик качал бы сам автор.
    published = Q(material__status=Moderated.Status.APPROVED) | Q(book__status=Moderated.Status.APPROVED)
    rows = File.objects.filter(
        published, uploader=user, downloads__gte=DOWNLOADS_PER_COIN,
    ).values_list("downloads", flat=True)
    total = sum(min(count // DOWNLOADS_PER_COIN, DOWNLOAD_CAP) for count in rows)
    yield from _batches(R.DOWNLOAD, total, DOWNLOAD_BATCH, "скачивают твои файлы")


def _batches(reason, total, size, note):
    """Награда порциями — по строке на порцию, ключ — номер порции. Записанная строка не
    меняется. Номера только прибавляются: уменьшение не отбирает и заново не платит — поэтому
    `remove` эти награды не трогает."""
    for number in range(1, total // size + 1):
        yield Award(reason, str(number), size, note)


def _wall(user):
    """По WallProfile.painted, а не по журналу доски: там и заливки модератора, и консоль.
    Порциями, как скачивания (`_batches`)."""
    profile = getattr(user, "wall", None)
    if profile:
        yield from _batches(R.WALL, profile.painted, WALL_BATCH, "клетки на Стене")


def _quest(user):
    """Ключ от взлома, введённый в консоли отсчёта (quest.keys.unlock)."""
    from quest.models import Unlock

    if Unlock.objects.filter(user=user).exists():
        yield Award(R.QUEST, "glitch-2026", QUEST_KEY, "ключ от взлома")


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
    """Дописать недостающее; вернуть начисленное по причинам. Звать после любого события
    вклада. Кошелёк — сразу, до чтения журнала: иначе два вызова разом заплатили бы дважды."""
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
    """Что выплачено автору за вещь и ещё не забрано: ([(причина, ключ, сумма)], подпись). По
    журналу: начисленное минус забранное — повторный возврат ничего не находит."""
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
    """Удалить вещь и забрать выплаченное, если удаляет сам автор. Возвращает, сколько забрали.

    Чужое действие (модератор, снятый лайк) ничего не стоит. Своё удаление без возврата —
    ферма: новая вещь — новый ключ. Баланс может уйти в минус (services.reclaim). Одним
    действием с удалением: после него у вещи нет номера, по которому искать выплаты.
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
    """Хвост сообщения об удалении: о списании человек узнаёт сразу."""
    if not taken:
        return ""
    from core.templatetags.text_extras import plural

    return f" Списано {taken} {plural(taken, 'токен,токена,токенов')} — награда за удалённое."
