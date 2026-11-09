from django.db.models import F
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from comments.models import Comment

from .models import Notification
from .services import ring


@receiver(pre_delete, sender=Comment)
def comment_gone(sender, instance, **kwargs):
    """Удалённый комментарий забирает своё уведомление; из склеенной строки — только себя.

    Ответы уходят каскадом вместе с корнем — сигнал приходит и на каждый из них.
    """
    touched = set(Notification.objects.filter(comment=instance).values_list("recipient_id", flat=True))
    Notification.objects.filter(comment=instance, times__lte=1).delete()
    Notification.objects.filter(comment=instance).update(times=F("times") - 1, comment=None)
    ring(*touched)
