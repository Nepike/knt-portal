"""Пересчёт на входе: стартовые находят и заведённого в админке; заодно подбирается недосчитанное."""

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from . import rewards


@receiver(user_logged_in)
def pay_on_login(sender, request, user, **kwargs):
    rewards.sync(user)
