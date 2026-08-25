from io import BytesIO, StringIO

from django.core.management import call_command
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from django.core.files.uploadedfile import SimpleUploadedFile

from PIL import Image as PilImage

from attachments.models import File
from core.models import Subject
from materials.models import Comment, Material
from teachers.models import Review, Teacher
from users.models import User
from wall.models import WallProfile

from . import rewards
from .admin import GrantForm
from .models import BalanceLog, Wallet
from .services import NotEnoughFunds, credit, recount, spend, wallet_of

MANUAL = BalanceLog.Reason.MANUAL
SPENT = BalanceLog.Reason.MANUAL  # трат по правилам пока нет — списываем вручную


def make_png(name="картинка.png"):
    """Настоящий PNG: ImageField проверяет содержимое, подделка из байтов не пройдёт."""
    buffer = BytesIO()
    PilImage.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


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
        spend(self.user, 30, SPENT)
        self.assertEqual(wallet_of(self.user).balance, 70)

    def test_journal_keeps_signs_and_running_balance(self):
        credit(self.user, 100, MANUAL)
        spend(self.user, 30, SPENT)
        entries = list(BalanceLog.objects.order_by("id").values_list("amount", "balance_after"))
        self.assertEqual(entries, [(100, 100), (-30, 70)])

    def test_spending_more_than_there_is_changes_nothing(self):
        credit(self.user, 10, MANUAL)
        with self.assertRaises(NotEnoughFunds):
            spend(self.user, 11, SPENT)
        self.assertEqual(wallet_of(self.user).balance, 10)
        self.assertEqual(BalanceLog.objects.count(), 1)

    def test_spending_everything_is_allowed(self):
        credit(self.user, 10, MANUAL)
        spend(self.user, 10, SPENT)
        self.assertEqual(wallet_of(self.user).balance, 0)

    def test_wrong_sign_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            credit(self.user, -5, MANUAL)
        with self.assertRaises(ValueError):
            spend(self.user, 0, SPENT)

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
        # Только по этому кошельку: вход самого модератора в админку тоже оставил строку.
        self.assertEqual(BalanceLog.objects.get(wallet=self.wallet).balance_after, 250)

    def test_overdraft_is_refused_by_the_form(self):
        response = self.post(-5)
        self.assertContains(response, "на балансе только 0")
        self.assertEqual(BalanceLog.objects.filter(wallet=self.wallet).count(), 0)

    def test_only_manual_reasons_are_offered_by_hand(self):
        """Награда руками — это строка без ключа, она зачлась бы как «уже выплачено»;
        покупку пишет магазин вместе с выдачей вещи. См. BalanceLog.BY_HAND."""
        offered = {value for value, _ in GrantForm().fields["reason"].choices}
        self.assertEqual(offered, {MANUAL})


class LoginRewardTests(TestCase):
    """Стартовые обязаны находить человека сами: заведённый в админке иначе заходил бы
    на пустой кошелёк и не смог купить в магазине ничего."""

    def test_the_welcome_grant_lands_on_the_first_login(self):
        user = make_user()
        self.assertFalse(Wallet.objects.filter(user=user).exists())

        self.client.force_login(user)

        self.assertEqual(wallet_of(user).balance, rewards.WELCOME)

    def test_logging_in_again_does_not_pay_twice(self):
        user = make_user()
        self.client.force_login(user)
        self.client.force_login(user)

        self.assertEqual(wallet_of(user).balance, rewards.WELCOME)


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


class RewardTests(TestCase):
    """Начисления. Правило одно: сколько положено — функция от состояния, журнал
    помнит выплаченное, sync дописывает разницу."""

    @classmethod
    def setUpTestData(cls):
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")

    def setUp(self):
        self.user = make_user()

    def material(self, status=Material.Status.APPROVED, uploader=None):
        return Material.objects.create(
            title="Конспект", subject=self.subject, uploader=uploader or self.user, status=status,
        )

    def paid(self, reason):
        wallet = Wallet.objects.filter(user=self.user).first()
        rows = wallet.entries.filter(reason=reason, amount__gt=0) if wallet else []
        return sum(row.amount for row in rows)

    def test_everyone_gets_the_welcome_grant(self):
        # Иначе у 253 человек из 328 не было бы ни одной покупки: они ничего не заливали.
        rewards.sync(self.user)

        self.assertEqual(wallet_of(self.user).balance, rewards.WELCOME)

    def test_running_twice_changes_nothing(self):
        self.material()
        rewards.sync(self.user)
        was = BalanceLog.objects.count()

        self.assertEqual(rewards.sync(self.user), {})
        self.assertEqual(BalanceLog.objects.count(), was)

    def test_only_published_work_is_paid_for(self):
        self.material()
        self.material(status=Material.Status.PENDING)
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.MATERIAL), rewards.MATERIAL)

    def test_a_review_with_text_is_worth_more_than_bare_scores(self):
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        other = Teacher.objects.create(name="Анна", surname="Сидорова")
        Review.objects.create(teacher=teacher, author=self.user, text="подробно")
        Review.objects.create(teacher=other, author=self.user, score_knowledge=5)
        rewards.sync(self.user)

        self.assertEqual(
            self.paid(BalanceLog.Reason.REVIEW), rewards.REVIEW_TEXT + rewards.REVIEW_SCORES,
        )

    def test_a_deleted_material_does_not_block_the_next_one(self):
        """Награда считается ПОШТУЧНО, а не суммой по причине.

        Иначе выходило бы так: человек удалил свой материал, залил новый — число
        материалов вернулось к прежнему, «положено» тоже, и за новую работу не заплатили.
        """
        first = self.material()
        rewards.sync(self.user)
        first.delete()

        self.material()
        rewards.sync(User.objects.get(pk=self.user.pk))

        self.assertEqual(self.paid(BalanceLog.Reason.MATERIAL), rewards.MATERIAL * 2)

    def test_a_review_with_a_picture_but_no_text_counts_as_a_full_one(self):
        # Сайт такой отзыв показывает всегда и даёт за него голосовать (Review.is_detailed),
        # значит и платить надо как за полный, а не как за голые оценки.
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        Review.objects.create(teacher=teacher, author=self.user, image=make_png())
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.REVIEW), rewards.REVIEW_TEXT)

    def test_a_comment_is_not_paid_for_by_itself(self):
        # Ни с текстом, ни с картинкой: иначе под каждым материалом выросла бы ферма «спасибо».
        material = self.material()
        Comment.objects.create(material=material, author=self.user, text="спасибо")
        Comment.objects.create(material=material, author=self.user, image=make_png())
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), 0)
        self.assertEqual(wallet_of(self.user).balance, rewards.WELCOME + rewards.MATERIAL)

    def test_likes_pay_the_author_net_of_dislikes(self):
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        review = Review.objects.create(teacher=teacher, author=self.user, text="подробно")
        review.liked_users.add(make_user("a@t.local"), make_user("b@t.local"), make_user("c@t.local"))
        review.disliked_users.add(make_user("d@t.local"))
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE * 2)

    def test_a_disliked_review_never_goes_into_debt(self):
        # Иначе первый же дизлайк отбивал бы охоту писать вообще.
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        review = Review.objects.create(teacher=teacher, author=self.user, text="спорно")
        review.disliked_users.add(make_user("d@t.local"), make_user("e@t.local"))
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), 0)

    def test_likes_on_one_entry_are_capped(self):
        # Десяток друзей иначе превратил бы одну запись в основной доход.
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        review = Review.objects.create(teacher=teacher, author=self.user, text="популярно")
        review.liked_users.add(*[make_user(f"{n}@t.local") for n in range(rewards.LIKE_CAP + 5)])
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE * rewards.LIKE_CAP)

    def test_taking_a_like_back_does_not_take_the_tokens_back(self):
        # Иначе снять и поставить лайк заново было бы бесконечной фермой.
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        review = Review.objects.create(teacher=teacher, author=self.user, text="подробно")
        fan = make_user("a@t.local")
        review.liked_users.add(fan)
        rewards.sync(self.user)

        review.liked_users.remove(fan)
        rewards.sync(User.objects.get(pk=self.user.pk))
        review.liked_users.add(fan)
        rewards.sync(User.objects.get(pk=self.user.pk))

        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE)

    def test_downloads_are_capped_per_file(self):
        # Счётчик лежит на файле, кто скачал — нигде: без потолка накрутка окупалась бы.
        over = rewards.DOWNLOAD_CAP * rewards.DOWNLOADS_PER_COIN * 10
        material = self.material()
        File.objects.create(material=material, name="a", file="a.pdf", size=1, uploader=self.user, downloads=over)
        File.objects.create(material=material, name="b", file="b.pdf", size=1, uploader=self.user, downloads=over)
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.DOWNLOAD), rewards.DOWNLOAD_CAP * 2)

    def test_the_wall_pays_in_batches(self):
        profile = WallProfile.objects.create(user=self.user, painted=rewards.WALL_BATCH + 3)
        rewards.sync(self.user)
        self.assertEqual(self.paid(BalanceLog.Reason.WALL), rewards.WALL_BATCH)

        profile.painted = rewards.WALL_BATCH * 2
        profile.save(update_fields=["painted"])
        rewards.sync(User.objects.get(pk=self.user.pk))

        self.assertEqual(self.paid(BalanceLog.Reason.WALL), rewards.WALL_BATCH * 2)

    def test_moderating_your_own_work_pays_nothing(self):
        # Иначе модератор получал бы дважды: и как автор, и как проверяющий.
        mine = self.material()
        mine.reviewed_by = self.user
        mine.save(update_fields=["reviewed_by"])
        theirs = self.material(uploader=make_user("o@t.local"))
        theirs.reviewed_by = self.user
        theirs.save(update_fields=["reviewed_by"])
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.MODERATION), rewards.MODERATION)

    def test_spending_does_not_bring_the_reward_back(self):
        # Награда считается по начислениям, а не по балансу: иначе трата обнуляла бы
        # выплаченное и следующий пересчёт начислил бы всё заново.
        self.material()
        rewards.sync(self.user)
        spend(self.user, rewards.WELCOME, SPENT)

        self.assertEqual(rewards.sync(User.objects.get(pk=self.user.pk)), {})


class RecountCommandTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def run_it(self, *args):
        out = StringIO()
        call_command("recount_tokens", *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        output = self.run_it()

        self.assertEqual(BalanceLog.objects.count(), 0)
        self.assertIn("Пробный прогон", output)

    def test_apply_credits_everyone(self):
        self.run_it("--apply")

        self.assertEqual(wallet_of(self.user).balance, rewards.WELCOME)

    def test_running_it_again_changes_nothing(self):
        self.run_it("--apply")
        was = BalanceLog.objects.count()

        self.run_it("--apply")

        self.assertEqual(BalanceLog.objects.count(), was)
        self.assertEqual(wallet_of(self.user).balance, rewards.WELCOME)

    def test_it_only_adds_and_never_takes_away(self):
        """Сноса журнала у команды нет: после открытия магазина он вернул бы токены
        за покупки, оставив людям и вещи."""
        credit(self.user, 5000, MANUAL)
        spend(self.user, 600, BalanceLog.Reason.PURCHASE, key="item:1")

        self.run_it("--apply")

        self.assertEqual(wallet_of(self.user).balance, 4400 + rewards.WELCOME)

    def test_an_inactive_person_is_skipped(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        self.run_it("--apply")

        self.assertEqual(BalanceLog.objects.count(), 0)
