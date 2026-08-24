"""Пересчёт на входе в систему.

Иначе стартовые получал бы только тот, за кем случилось хоть одно событие: заведённый
в админке человек заходил бы на пустой кошелёк и не мог купить ничего. Заодно
подбирается всё, что могло не досчитаться, — восемь запросов на вход это не цена.
"""

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from . import rewards


@receiver(user_logged_in)
def pay_on_login(sender, request, user, **kwargs):
    rewards.sync(user)
