from io import StringIO

from django.core.management import call_command
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from users.models import User

from .models import BalanceLog, Wallet
from .services import NotEnoughFunds, credit, recount, spend, wallet_of

MANUAL = BalanceLog.Reason.MANUAL
REROLL = BalanceLog.Reason.WALL_REROLL


def make_user(email="u@t.local"):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


def break_cache(user, value):
    """Портим кэш мимо сервиса — так это и выглядело бы при чужой правке баланса."""
    Wallet.objects.filter(user=user).update(balance=value)


class BalanceTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_first_operation_creates_the_wallet(self):
        credit(self.user, 50, MANUAL)
        self.assertEqual(Wallet.objects.get(user=self.user).balance, 50)

    def test_credit_and_spend_move_the_balance(self):
        credit(self.user, 100, MANUAL)
        spend(self.user, 30, REROLL)
        self.assertEqual(wallet_of(self.user).balance, 70)

    def test_journal_keeps_signs_and_running_balance(self):
        credit(self.user, 100, MANUAL)
        spend(self.user, 30, REROLL)
        entries = list(BalanceLog.objects.order_by("id").values_list("amount", "balance_after"))
        self.assertEqual(entries, [(100, 100), (-30, 70)])

    def test_spending_more_than_there_is_changes_nothing(self):
        credit(self.user, 10, MANUAL)
        with self.assertRaises(NotEnoughFunds):
            spend(self.user, 11, REROLL)
        self.assertEqual(wallet_of(self.user).balance, 10)
        self.assertEqual(BalanceLog.objects.count(), 1)

    def test_spending_everything_is_allowed(self):
        credit(self.user, 10, MANUAL)
        spend(self.user, 10, REROLL)
        self.assertEqual(wallet_of(self.user).balance, 0)

    def test_wrong_sign_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            credit(self.user, -5, MANUAL)
        with self.assertRaises(ValueError):
            spend(self.user, 0, REROLL)

    def test_saving_the_user_does_not_touch_the_balance(self):
        """Ради этого кошелёк и вынесен из User: user.save() пишет все поля разом."""
        stale = User.objects.get(pk=self.user.pk)
        credit(self.user, 40, MANUAL)
        stale.save()
        self.assertEqual(wallet_of(self.user).balance, 40)


class RecountTests(TestCase):
    def test_recount_repairs_a_broken_cache(self):
        user = make_user()
        credit(user, 100, MANUAL)
        break_cache(user, 7)
        self.assertEqual(recount(wallet_of(user)), (7, 100))
        self.assertEqual(wallet_of(user).balance, 100)

    def test_empty_wallet_counts_as_zero(self):
        self.assertEqual(recount(wallet_of(make_user())), (0, 0))

    def test_dry_run_reports_but_does_not_fix(self):
        user = make_user()
        credit(user, 100, MANUAL)
        break_cache(user, 7)
        out = StringIO()
        call_command("recount_balances", stdout=out)
        self.assertIn("по журналу 100", out.getvalue())
        self.assertEqual(wallet_of(user).balance, 7)

    def test_apply_fixes_the_cache(self):
        user = make_user()
        credit(user, 100, MANUAL)
        break_cache(user, 7)
        call_command("recount_balances", "--apply", stdout=StringIO())
        self.assertEqual(wallet_of(user).balance, 100)


class AdminGrantTests(TestCase):
    """Форма журнала в админке — это ручная выдача валюты, и она обязана идти через сервис."""

    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.create_superuser(
            email="boss@t.local", name="Босс", surname="Главный", password="pass12345",
        )
        cls.user = make_user()

    def setUp(self):
        self.client.force_login(self.boss)
        self.wallet = wallet_of(self.user)

    def post(self, amount):
        return self.client.post(reverse("admin:economy_balancelog_add"), {
            "wallet": self.wallet.pk, "amount": amount, "reason": MANUAL, "note": "за помощь",
        })

    def test_adding_an_entry_moves_the_balance(self):
        self.assertEqual(self.post(250).status_code, 302)
        self.assertEqual(wallet_of(self.user).balance, 250)
        self.assertEqual(BalanceLog.objects.get().balance_after, 250)

    def test_overdraft_is_refused_by_the_form(self):
        response = self.post(-5)
        self.assertContains(response, "на балансе только 0")
        self.assertEqual(BalanceLog.objects.count(), 0)


class TagTests(TestCase):
    """Баланс в шапке сайдбара: у кошелька может не быть строки, и это ноль."""

    def render(self, person):
        return Template("{% load economy_extras %}{% coins person %}").render(Context({"person": person}))

    def test_a_newcomer_without_a_wallet_has_nothing(self):
        self.assertEqual(self.render(make_user()), "0")

    def test_the_balance_is_shown(self):
        user = make_user()
        credit(user, 250, MANUAL)
        self.assertEqual(self.render(User.objects.get(pk=user.pk)), "250")
