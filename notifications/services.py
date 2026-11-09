"""Откуда берутся уведомления. Зовут явно из мест, где случилось событие, — как награды
(`economy.rewards.sync`), а не сигналами: сигнал на `post_save` не знает, кто и зачем
сохранял, а правка комментария или смена статуса из админки уведомлять не должны.

Себе уведомление не приходит никогда. Любая перемена — новое, склеенное, прочитанное —
звонит по сокету человека (`ring`), и все его вкладки переспрашивают число.
"""

import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from chats import events

from .models import Notification

logger = logging.getLogger(__name__)

Kind = Notification.Kind


def commented(comment):
    """Новый комментарий: автору вещи и тому, кому ответили. Автор вещи, которому
    ответили под его же вещью, получает одно — «ответ», а не два."""
    thing = {comment.kind: comment.owner}
    owner_author = comment.owner.uploader if comment.material_id else comment.owner.playlist.uploader
    answered = comment.parent.author if comment.parent_id else None
    if answered and answered != comment.author:
        _gather(answered, Kind.REPLY, comment, thing)
    if owner_author and owner_author not in (comment.author, answered):
        _gather(owner_author, Kind.COMMENT, comment, thing)


def _gather(recipient, kind, comment, thing):
    fresh = Notification.objects.filter(recipient=recipient, kind=kind, read_at=None, **thing).first()
    if fresh is None:
        Notification.objects.create(
            recipient=recipient, kind=kind, actor=comment.author, anonymous=comment.hide_author, comment=comment, **thing,
        )
    else:
        fresh.times += 1
        fresh.actor, fresh.anonymous, fresh.comment = comment.author, comment.hide_author, comment
        fresh.created = timezone.now()  # свежее — наверх списка
        fresh.save(update_fields=["times", "actor", "anonymous", "comment", "created"])
    ring(recipient.pk)


def reviewed(item, moderator):
    """Решение модератора по материалу, книге или курсу — автору. Модератор, решивший
    про своё, узнал и так."""
    author = item.uploader
    if author is None or author == moderator:
        return
    Notification.objects.create(
        recipient=author, kind=Kind.APPROVED if item.is_published else Kind.REJECTED,
        actor=moderator, note=item.review_note, **{item._meta.model_name: item},
    )
    ring(author.pk)


def baked(job):
    _bake(job, Kind.BAKED)


def bake_failed(job):
    _bake(job, Kind.BAKE_FAILED)


def _bake(job, kind):
    """Выпечка лекции — автору курса: сама запись автора не хранит, а курс — да."""
    lecture = job.lecture
    author = lecture.playlist.uploader if lecture else None
    if author is not None:
        Notification.objects.create(recipient=author, kind=kind, note=job.note, lecture=lecture)
        ring(author.pk)


def opened(notification):
    """Нажал на уведомление."""
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
        ring(notification.recipient_id)


def seen(user, **thing):
    """Открыл вещь сам — всё про неё прочитано."""
    _read(user, Q(**thing))


def seen_course(user, playlist, watching=None):
    """Страница курса: прочитано всё про курс, выпечка любой его лекции (список с
    состояниями тут же) и обсуждение той лекции, что открыта."""
    about = Q(playlist=playlist) | Q(lecture__playlist=playlist, kind__in=[Kind.BAKED, Kind.BAKE_FAILED])
    if watching is not None:
        about |= Q(lecture=watching)
    _read(user, about)


def read_all(user):
    _read(user, Q())


def unread(user):
    return Notification.objects.filter(recipient=user, read_at=None)


def _read(user, about):
    # Звоним, только если правда что-то прочиталось: страницы материалов открывают
    # постоянно, и пустой звонок на каждую гонял бы вкладки за тем же числом.
    if unread(user).filter(about).update(read_at=timezone.now()):
        ring(user.pk)


def announce(announcement, people):
    """Объявление — каждому строкой: колокольчик, «прочитано» и уборка у них общие с
    остальными уведомлениями. Сколько получило."""
    ids = list(people.values_list("pk", flat=True))
    Notification.objects.bulk_create(
        Notification(recipient_id=pk, kind=Kind.ANNOUNCEMENT, actor=announcement.author, announcement=announcement)
        for pk in ids
    )
    ring(*ids)
    return len(ids)


def withdraw(announcement):
    """Снять объявление у всех. Звоним тем, у кого оно висело непрочитанным: число у них
    уменьшится, остальным звонить незачем."""
    waiting = list(announcement.notifications.filter(read_at=None).values_list("recipient_id", flat=True))
    announcement.delete()
    ring(*waiting)


def ring(*user_ids):
    """Повод вкладкам людей переспросить число — после коммита, иначе они спросили бы
    раньше, чем строка появилась. Лежит Redis — молчим: число подтянется со следующей
    страницей, а комментарий из-за колокольчика не должен падать."""
    if not user_ids:
        return

    def publish():
        try:
            events.notify_notifications(*user_ids)
        except Exception:  # noqa: BLE001 — какой именно, зависит от слоя каналов
            logger.exception("Слой каналов недоступен, колокольчик не обновлён у %s человек", len(user_ids))

    transaction.on_commit(publish)
