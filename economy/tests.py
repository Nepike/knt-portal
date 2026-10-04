import json
from io import BytesIO, StringIO
from unittest import mock

from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from django.core.files.uploadedfile import SimpleUploadedFile

from PIL import Image as PilImage

from attachments.models import File
from chats.models import Chat, Message
from core.models import Subject
from comments.models import Comment
from lectorium.models import Playlist
from library.models import Book
from materials.models import Material
from teachers.models import Review, Teacher
from users.models import User
from wall.models import WallProfile

from . import rewards
from .admin import GrantForm
from .models import BalanceLog, Wallet
from .services import NotEnoughFunds, credit, reclaim, recount, spend, wallet_of

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
        with self.assertRaises(ValueError):
            reclaim(self.user, -5, MANUAL)

    def test_taking_a_reward_back_may_leave_a_debt(self):
        """Иначе «получил награду, потратил, удалил» оставалось бы бесплатным."""
        credit(self.user, 10, MANUAL)
        reclaim(self.user, 30, MANUAL)

        self.assertEqual(wallet_of(self.user).balance, -20)
        self.assertEqual(BalanceLog.objects.first().balance_after, -20)

    def test_nothing_is_bought_on_credit(self):
        """В минус уводит только возврат. Трата по-прежнему упирается в ноль — и тем
        более не идёт из минуса."""
        credit(self.user, 10, MANUAL)
        with self.assertRaises(NotEnoughFunds):
            spend(self.user, 11, SPENT)

        reclaim(self.user, 30, MANUAL)
        with self.assertRaises(NotEnoughFunds):
            spend(self.user, 1, SPENT)
        self.assertEqual(wallet_of(self.user).balance, -20)

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

    def test_a_wallet_in_debt_is_repaired_like_any_other(self):
        """Минус по журналу — не поломка, а возврат награды, которую успели потратить."""
        user = make_user()
        credit(user, 10, MANUAL)
        reclaim(user, 30, MANUAL)
        break_cache(user, 7)

        call_command("recount_balances", "--apply", stdout=StringIO())

        self.assertEqual(wallet_of(user).balance, -20)


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

    def test_a_wallet_in_debt_can_still_be_granted_to(self):
        """Проверка «хватит ли» касается только списания: иначе человеку в минусе
        нельзя было бы начислить ничего меньше самого долга."""
        reclaim(self.user, 50, MANUAL)

        self.assertEqual(self.post(20).status_code, 302)
        self.assertEqual(wallet_of(self.user).balance, -30)

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

    def fans(self, count, tag="fan"):
        return [make_user(f"{tag}{number}@t.local") for number in range(count)]

    def test_likes_pay_the_author_net_of_dislikes(self):
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        review = Review.objects.create(teacher=teacher, author=self.user, text="подробно")
        review.liked_users.add(*self.fans(rewards.LIKE_FROM + 2))
        review.disliked_users.add(make_user("d@t.local"))
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE * (rewards.LIKE_FROM + 1))

    def test_likes_pay_nothing_until_there_are_enough_of_them(self):
        """Двое друзей иначе лайкали бы друг другу комментарии без конца. А как набралось —
        платим сразу за все, и дальше за каждый."""
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        review = Review.objects.create(teacher=teacher, author=self.user, text="подробно")
        fans = self.fans(rewards.LIKE_FROM + 1)

        review.liked_users.add(*fans[:rewards.LIKE_FROM - 1])
        rewards.sync(self.user)
        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), 0)

        review.liked_users.add(fans[rewards.LIKE_FROM - 1])
        rewards.sync(User.objects.get(pk=self.user.pk))
        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE * rewards.LIKE_FROM)

        review.liked_users.add(fans[rewards.LIKE_FROM])
        rewards.sync(User.objects.get(pk=self.user.pk))
        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE * (rewards.LIKE_FROM + 1))
        self.assertEqual(rewards.LIKE_FROM, 5)  # так условились; порог в один — это его отсутствие

    def test_your_own_like_brings_nothing(self):
        """Поставить лайк себе можно, но в счёт он не идёт — ни к порогу, ни к сумме."""
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        review = Review.objects.create(teacher=teacher, author=self.user, text="подробно")
        review.liked_users.add(self.user, *self.fans(rewards.LIKE_FROM - 1))  # со своим — ровно порог
        rewards.sync(self.user)
        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), 0)

        review.liked_users.add(make_user("last@t.local"))
        rewards.sync(User.objects.get(pk=self.user.pk))
        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE * rewards.LIKE_FROM)

    def test_your_own_dislike_does_not_count_either(self):
        teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        review = Review.objects.create(teacher=teacher, author=self.user, text="подробно")
        review.liked_users.add(*self.fans(rewards.LIKE_FROM))
        review.disliked_users.add(self.user)
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE * rewards.LIKE_FROM)

    def test_a_comment_follows_the_same_rule(self):
        comment = Comment.objects.create(material=self.material(), author=self.user, text="разбор")
        comment.liked_users.add(self.user, *self.fans(rewards.LIKE_FROM - 1))
        rewards.sync(self.user)
        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), 0)

        comment.liked_users.add(make_user("last@t.local"))
        rewards.sync(User.objects.get(pk=self.user.pk))
        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE * rewards.LIKE_FROM)

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
        fans = self.fans(rewards.LIKE_FROM)
        review.liked_users.add(*fans)
        rewards.sync(self.user)

        review.liked_users.remove(fans[0])
        rewards.sync(User.objects.get(pk=self.user.pk))
        review.liked_users.add(fans[0])
        rewards.sync(User.objects.get(pk=self.user.pk))

        self.assertEqual(self.paid(BalanceLog.Reason.LIKES), rewards.LIKE * rewards.LIKE_FROM)

    def downloaded(self, *counts):
        material = self.material()
        for number, count in enumerate(counts):
            File.objects.create(
                material=material, name=f"f{number}", file=f"f{number}.pdf",
                size=1, uploader=self.user, downloads=count,
            )

    def test_only_published_work_counts_towards_downloads(self):
        """Вложение чата и файл черновика видит один автор — и скачивал бы их он же."""
        enough = rewards.DOWNLOAD_BATCH * rewards.DOWNLOADS_PER_COIN
        book = Book.objects.create(title="Черновик", uploader=self.user, status=Book.Status.PENDING)
        message = Message.objects.create(
            chat=Chat.objects.create(kind="group", title="Болталка"), author=self.user, text="держи",
        )
        owners = ({"material": self.material(status=Material.Status.PENDING)}, {"book": book}, {"message": message})
        for owner in owners:
            File.objects.create(name="f", file="f.pdf", size=1, uploader=self.user, downloads=enough, **owner)
        rewards.sync(self.user)
        self.assertEqual(self.paid(BalanceLog.Reason.DOWNLOAD), 0)

        Book.objects.filter(pk=book.pk).update(status=Book.Status.APPROVED)
        rewards.sync(User.objects.get(pk=self.user.pk))
        self.assertEqual(self.paid(BalanceLog.Reason.DOWNLOAD), rewards.DOWNLOAD_BATCH)

    def test_downloads_are_capped_per_file(self):
        # Счётчик лежит на файле, кто скачал — нигде: без потолка накрутка окупалась бы.
        over = rewards.DOWNLOAD_CAP * rewards.DOWNLOADS_PER_COIN * 10
        self.downloaded(over, over)
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.DOWNLOAD), rewards.DOWNLOAD_CAP * 2)

    def test_downloads_of_different_files_add_up(self):
        """Награда не на файл: иначе журнал зарастал столбиком «+1 скачивают
        «Программа.pdf»» — 20562 строки на боевых данных."""
        each = rewards.DOWNLOAD_BATCH * rewards.DOWNLOADS_PER_COIN // 2  # по половине порции
        self.downloaded(each, each)
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.DOWNLOAD), rewards.DOWNLOAD_BATCH)
        self.assertEqual(BalanceLog.objects.filter(reason=BalanceLog.Reason.DOWNLOAD).count(), 1)

    def rows(self, reason):
        return list(
            BalanceLog.objects.filter(wallet__user=self.user, reason=reason)
            .order_by("id").values_list("key", "amount")
        )

    def test_every_batch_gets_its_own_line_of_exactly_one_batch(self):
        """Строка журнала — запись о случившемся, она не должна расти. Ключ у порции
        её номер, поэтому четыре полусотни это четыре строки по 50, а не одна на 200."""
        step = rewards.DOWNLOAD_BATCH * rewards.DOWNLOADS_PER_COIN
        self.downloaded(step, step, step, step)  # ровно четыре порции

        rewards.sync(self.user)

        self.assertEqual(self.rows(BalanceLog.Reason.DOWNLOAD), [
            ("1", rewards.DOWNLOAD_BATCH), ("2", rewards.DOWNLOAD_BATCH),
            ("3", rewards.DOWNLOAD_BATCH), ("4", rewards.DOWNLOAD_BATCH),
        ])

    def test_an_already_written_line_never_changes(self):
        """Даже если человек не заходил полгода и набежало сразу четыре порции —
        прежние строки остаются как были, новые приписываются следом."""
        step = rewards.DOWNLOAD_BATCH * rewards.DOWNLOADS_PER_COIN
        self.downloaded(step)
        rewards.sync(self.user)
        first = self.rows(BalanceLog.Reason.DOWNLOAD)

        File.objects.filter(uploader=self.user).update(downloads=step)
        self.downloaded(step, step, step)
        rewards.sync(User.objects.get(pk=self.user.pk))

        after = self.rows(BalanceLog.Reason.DOWNLOAD)
        self.assertEqual(after[:1], first)  # первая строка не тронута
        self.assertEqual(len(after), 4)
        self.assertEqual({amount for _, amount in after}, {rewards.DOWNLOAD_BATCH})

    def test_the_wall_pays_a_line_per_batch_too(self):
        WallProfile.objects.create(user=self.user, painted=rewards.WALL_BATCH * 3)

        rewards.sync(self.user)

        self.assertEqual(self.rows(BalanceLog.Reason.WALL), [
            ("1", rewards.WALL_BATCH), ("2", rewards.WALL_BATCH), ("3", rewards.WALL_BATCH),
        ])

    def test_downloads_pay_in_batches(self):
        step = rewards.DOWNLOAD_BATCH * rewards.DOWNLOADS_PER_COIN
        self.downloaded(step - 5)  # порог не взят
        rewards.sync(self.user)
        self.assertEqual(self.paid(BalanceLog.Reason.DOWNLOAD), 0)

        File.objects.filter(uploader=self.user).update(downloads=step + 5)
        rewards.sync(User.objects.get(pk=self.user.pk))

        self.assertEqual(self.paid(BalanceLog.Reason.DOWNLOAD), rewards.DOWNLOAD_BATCH)

    def test_the_remainder_is_not_lost_it_waits(self):
        """Остаток ниже порога не пропадает: он копится и уходит следующей порцией.

        Файлов тут два, потому что порция равна потолку на файл: одним больше 50 токенов
        не заработать, и полторы порции набираются только вдвоём.
        """
        step = rewards.DOWNLOAD_BATCH * rewards.DOWNLOADS_PER_COIN
        self.downloaded(step, step // 2)  # порция с половиной
        rewards.sync(self.user)
        self.assertEqual(self.paid(BalanceLog.Reason.DOWNLOAD), rewards.DOWNLOAD_BATCH)

        File.objects.filter(uploader=self.user, name="f1").update(downloads=step)
        rewards.sync(User.objects.get(pk=self.user.pk))

        self.assertEqual(self.paid(BalanceLog.Reason.DOWNLOAD), rewards.DOWNLOAD_BATCH * 2)

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

    def course(self, status=Playlist.Status.APPROVED, uploader=None, title="Линейная алгебра"):
        return Playlist.objects.create(
            title=title, subject=self.subject, uploader=uploader or self.user, status=status,
        )

    def test_an_approved_course_is_worth_ten_materials(self):
        """Снять пару, дотащить гигабайты до сайта и дождаться выпечки — работа другого
        порядка, чем выложить конспект."""
        self.course()
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.PLAYLIST), rewards.PLAYLIST)
        self.assertEqual(rewards.PLAYLIST, rewards.MATERIAL * 10)

    def test_a_course_on_review_is_not_paid_for_yet(self):
        self.course(status=Playlist.Status.PENDING)
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.PLAYLIST), 0)

    def test_each_course_is_paid_for_separately(self):
        # Ключ у награды свой на каждый курс: удаливший один не лишается платы за другой.
        self.course(title="Первый")
        self.course(title="Второй")
        rewards.sync(self.user)

        self.assertEqual(self.paid(BalanceLog.Reason.PLAYLIST), rewards.PLAYLIST * 2)

    def test_a_course_that_vanished_by_itself_does_not_take_its_payment_back(self):
        """Курс исчез не рукой автора (каскад, чистка, модератор) — выплаченное остаётся.
        Назад награду забирает только сам автор, см. TakeBackTests."""
        course = self.course()
        rewards.sync(self.user)
        course.delete()

        self.assertEqual(rewards.sync(User.objects.get(pk=self.user.pk)), {})
        self.assertEqual(self.paid(BalanceLog.Reason.PLAYLIST), rewards.PLAYLIST)

    def test_checking_someone_elses_course_pays_the_moderator(self):
        theirs = self.course(uploader=make_user("lect@t.local"))
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

    def test_people_are_named_surname_first_as_on_the_site(self):
        self.assertIn("Иванов Иван", self.run_it())

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


class WalletPageTests(TestCase):
    """Полная история кошелька. Отдельная страница нужна была профилю: там влезает
    десяток последних операций, а по журналу человек ищет конкретную."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_it_shows_the_journal(self):
        credit(self.user, 50, BalanceLog.Reason.MATERIAL, note="Конспект по матанализу", key="1")

        page = self.client.get(reverse("wallet")).content.decode()

        self.assertIn("Конспект по матанализу", page)
        self.assertIn("+50", page)

    def test_it_does_not_show_anybody_elses(self):
        stranger = make_user("other@t.local")
        credit(stranger, 50, BalanceLog.Reason.MATERIAL, note="Чужая работа", key="1")

        page = self.client.get(reverse("wallet")).content.decode()

        self.assertNotIn("Чужая работа", page)

    def test_an_empty_journal_is_not_an_error(self):
        """Вход начисляет стартовые, поэтому пустой журнал наяву почти не встречается —
        но страница обязана открываться и без него, а не падать на пустом списке."""
        BalanceLog.objects.all().delete()

        response = self.client.get(reverse("wallet"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("Операций пока не было", response.content.decode())

    def test_the_next_batch_arrives_without_the_page_around_it(self):
        for number in range(60):
            credit(self.user, 1, BalanceLog.Reason.MATERIAL, note=f"работа {number}", key=str(number))

        page = self.client.get(reverse("wallet"), {"page": 2}, headers={"HX-Request": "true"}).content.decode()

        self.assertIn("работа 0", page)  # самые старые — на второй странице
        self.assertNotIn("<html", page)

    def test_the_profile_links_to_it_and_stops_at_the_limit(self):
        from users.views import RECENT

        for number in range(RECENT + 2):
            credit(self.user, 1, BalanceLog.Reason.MATERIAL, note=f"работа {number}", key=str(number))

        page = self.client.get(reverse("profile", args=[self.user.pk])).content.decode()

        self.assertEqual(page.count("работа "), RECENT)
        self.assertIn(reverse("wallet"), page)

    def test_short_history_does_not_pretend_there_is_more(self):
        credit(self.user, 1, BalanceLog.Reason.MATERIAL, note="одна работа", key="1")

        page = self.client.get(reverse("profile", args=[self.user.pk])).content.decode()

        self.assertNotIn("Вся история …", page)


class RegroupMigrationTests(TestCase):
    """Миграция 0006: пофайловые строки «скачивают» пересобираются в строки-порции.

    Без неё прежние ключи `download|<файл>` не зачлись бы против новых `download|1`,
    `download|2`… и sync заплатил бы всем повторно — всю сумму целиком.
    """

    def run_migration(self):
        """Зовём саму функцию миграции, а не гоняем migrate: проверять надо ровно то,
        что поедет на бой, но на данных, заведённых в тесте."""
        import importlib

        from django.apps import apps

        module = importlib.import_module("economy.migrations.0006_collapse_download_entries")
        module.regroup(apps, None)

    def journal(self, user):
        return list(
            BalanceLog.objects.filter(wallet__user=user).order_by("id")
            .values_list("reason", "key", "amount", "balance_after")
        )

    def downloads(self, user):
        return [(key, amount) for reason, key, amount, _ in self.journal(user) if reason == "download"]

    def test_small_change_becomes_the_first_unfinished_batch(self):
        """Двадцать токенов на три файла — это ещё не порция. Складываем их в первую,
        и следующий пересчёт допишет её до полной полусотни, а не заплатит заново."""
        user = make_user()
        credit(user, 500, BalanceLog.Reason.WELCOME)
        for number, amount in ((7, 3), (9, 11), (12, 6)):
            credit(user, amount, BalanceLog.Reason.DOWNLOAD, note=f"скачивают «{number}»", key=str(number))
        was = wallet_of(user).balance

        self.run_migration()

        self.assertEqual(self.downloads(user), [("1", 3 + 11 + 6)])
        self.assertEqual(wallet_of(user).balance, was)  # баланс не тронут

    def test_a_big_history_becomes_lines_of_one_batch_each(self):
        user = make_user()
        for number in range(6):  # шесть файлов по потолку = 300 токенов = шесть порций
            credit(user, rewards.DOWNLOAD_CAP, BalanceLog.Reason.DOWNLOAD, key=str(number))

        self.run_migration()

        self.assertEqual(
            self.downloads(user),
            [(str(number), rewards.DOWNLOAD_BATCH) for number in range(1, 7)],
        )

    def test_lines_keep_their_place_in_the_ledger(self):
        """Строки переписываются поверх старых, а не создаются заново: журнал идёт
        по номеру строки, и новые уехали бы в конец, притворившись сегодняшними."""
        user = make_user()
        for number in range(4):
            credit(user, rewards.DOWNLOAD_CAP, BalanceLog.Reason.DOWNLOAD, key=str(number))
        credit(user, 50, BalanceLog.Reason.MATERIAL, key="1")  # операция ПОСЛЕ скачиваний
        was = [row[0] for row in self.journal(user)]

        self.run_migration()

        self.assertEqual([row[0] for row in self.journal(user)], was)

    def test_balance_after_stays_consistent_down_the_ledger(self):
        user = make_user()
        credit(user, 500, BalanceLog.Reason.WELCOME)
        credit(user, 10, BalanceLog.Reason.DOWNLOAD, key="1")
        credit(user, 50, BalanceLog.Reason.MATERIAL, key="1")  # строка МЕЖДУ скачиваниями
        credit(user, 20, BalanceLog.Reason.DOWNLOAD, key="2")

        self.run_migration()

        running = 0
        for _, _, amount, after in self.journal(user):
            running += amount
            self.assertEqual(after, running)

    def test_nobody_is_paid_twice_afterwards(self):
        user = make_user()
        material = Material.objects.create(
            title="Механика", year=2025, uploader=user, status=Material.Status.APPROVED,
            subject=Subject.objects.create(name="Физика", dative="физике", accusative="физику"),
        )
        step = rewards.DOWNLOAD_BATCH * rewards.DOWNLOADS_PER_COIN
        for number in range(2):
            File.objects.create(
                material=material, name=f"f{number}", file=f"f{number}.pdf",
                size=1, uploader=user, downloads=step,
            )
        # Как платил старый код: по строке на файл, по потолку на каждый.
        for number in range(2):
            credit(user, rewards.DOWNLOAD_CAP, BalanceLog.Reason.DOWNLOAD, key=str(number + 1))

        self.run_migration()
        rewards.sync(User.objects.get(pk=user.pk))  # заодно допишет стартовые и материал

        # Скачивания второй раз не оплачены: сумма та же, что заплатил старый код.
        self.assertEqual(
            self.downloads(user),
            [("1", rewards.DOWNLOAD_BATCH), ("2", rewards.DOWNLOAD_BATCH)],
        )


class TakeBackTests(TestCase):
    """Награда за вещь уходит назад, когда автор удаляет её САМ, — и только тогда.

    Без этого «написал — получил — удалил — написал заново» было фермой: у новой вещи
    новый ключ, и платили за неё заново.
    """

    @classmethod
    def setUpTestData(cls):
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.teacher = Teacher.objects.create(name="Пётр", surname="Петров")

    def setUp(self):
        self.user = make_user()
        self.other = make_user("other@t.local")

    def balance(self, user=None):
        return wallet_of(user or self.user).balance

    def sync(self, user=None):
        rewards.sync(User.objects.get(pk=(user or self.user).pk))

    def review(self):
        return Review.objects.create(teacher=self.teacher, author=self.user, text="подробно")

    def material(self, **extra):
        return Material.objects.create(**{
            "title": "Конспект", "subject": self.subject, "uploader": self.user,
            "status": Material.Status.APPROVED, **extra,
        })

    def fans(self):
        return [make_user(f"fan{number}@t.local") for number in range(rewards.LIKE_FROM)]

    def test_deleting_your_own_review_takes_its_reward_back(self):
        review = self.review()
        self.sync()

        taken = rewards.remove(review, by=self.user)

        self.assertEqual(taken, rewards.REVIEW_TEXT)
        self.assertFalse(Review.objects.exists())
        self.assertEqual(self.balance(), rewards.WELCOME)
        row = BalanceLog.objects.filter(wallet__user=self.user).first()
        self.assertEqual(
            (row.amount, row.reason, row.note),
            (-rewards.REVIEW_TEXT, BalanceLog.Reason.REVIEW, "удалён отзыв о Петров"),
        )

    def test_deleting_and_writing_again_earns_nothing(self):
        for _ in range(3):
            review = self.review()
            self.sync()
            rewards.remove(review, by=self.user)
        self.review()
        self.sync()

        self.assertEqual(self.balance(), rewards.WELCOME + rewards.REVIEW_TEXT)

    def test_someone_elses_hand_costs_the_author_nothing(self):
        """Модератор удалил — человек за это не платит: штрафовать за чужое действие не за что."""
        review = self.review()
        self.sync()

        self.assertEqual(rewards.remove(review, by=self.other), 0)

        self.assertFalse(Review.objects.exists())
        self.assertEqual(self.balance(), rewards.WELCOME + rewards.REVIEW_TEXT)
        self.assertFalse(BalanceLog.objects.filter(amount__lt=0).exists())

    def test_a_reward_already_spent_leaves_a_debt(self):
        """Иначе хватало бы потратить награду до удаления."""
        review = self.review()
        self.sync()
        spend(self.user, self.balance(), SPENT)

        rewards.remove(review, by=self.user)

        self.assertEqual(self.balance(), -rewards.REVIEW_TEXT)
        with self.assertRaises(NotEnoughFunds):
            spend(self.user, 1, SPENT)

    def test_likes_go_back_together_with_the_review(self):
        review = self.review()
        review.liked_users.add(*self.fans())
        self.sync()

        taken = rewards.remove(review, by=self.user)

        self.assertEqual(taken, rewards.REVIEW_TEXT + rewards.LIKE * rewards.LIKE_FROM)
        self.assertEqual(self.balance(), rewards.WELCOME)

    def test_a_comment_takes_the_authors_own_replies_along(self):
        """Ответы уезжают каскадом. Свои среди них автор удаляет тем же движением, а чужие
        исчезают не по воле своих авторов — им это ничего не стоит."""
        material = self.material(uploader=self.other)
        root = Comment.objects.create(material=material, author=self.user, text="корень")
        theirs = Comment.objects.create(material=material, author=self.other, text="ответ", parent=root)
        deep = Comment.objects.create(material=material, author=self.user, text="ответ на ответ", parent=theirs)
        fans = self.fans()
        for comment in (root, theirs, deep):
            comment.liked_users.add(*fans)
        self.sync()
        self.sync(self.other)
        theirs_before = self.balance(self.other)

        taken = rewards.remove(root, by=self.user)

        self.assertEqual(taken, 2 * rewards.LIKE * rewards.LIKE_FROM)
        self.assertEqual(self.balance(), rewards.WELCOME)
        self.assertFalse(Comment.objects.exists())
        self.assertEqual(self.balance(self.other), theirs_before)

    def test_a_material_a_book_and_a_course_go_back_too(self):
        things = [
            self.material(),
            Book.objects.create(title="Зорич", uploader=self.user, status=Book.Status.APPROVED),
            Playlist.objects.create(
                title="Матан", subject=self.subject, uploader=self.user, status=Playlist.Status.APPROVED,
            ),
        ]
        self.sync()
        self.assertEqual(
            self.balance(), rewards.WELCOME + rewards.MATERIAL + rewards.BOOK + rewards.PLAYLIST,
        )

        for thing in things:
            rewards.remove(thing, by=self.user)

        self.assertEqual(self.balance(), rewards.WELCOME)
        notes = BalanceLog.objects.filter(amount__lt=0).order_by("id").values_list("note", flat=True)
        self.assertEqual(
            list(notes), ["удалён материал «Конспект»", "удалена книга «Зорич»", "удалён курс «Матан»"],
        )

    def test_what_downloads_brought_stays(self):
        """Порции скачиваний — отметка «до скольки выплачено»: заново за них не платят,
        поэтому и забирать их незачем."""
        material = self.material()
        File.objects.create(
            material=material, name="f", file="f.pdf", size=1, uploader=self.user,
            downloads=rewards.DOWNLOAD_BATCH * rewards.DOWNLOADS_PER_COIN,
        )
        self.sync()

        rewards.remove(material, by=self.user)

        self.assertEqual(self.balance(), rewards.WELCOME + rewards.DOWNLOAD_BATCH)

    def test_what_was_never_paid_is_not_taken(self):
        draft = self.material(status=Material.Status.PENDING)
        self.sync()

        self.assertEqual(rewards.remove(draft, by=self.user), 0)
        self.assertFalse(BalanceLog.objects.filter(amount__lt=0).exists())

    def test_the_price_of_deleting_is_known_beforehand(self):
        review = self.review()
        review.liked_users.add(*self.fans())
        self.sync()

        self.assertEqual(
            rewards.at_stake(review, self.user), rewards.REVIEW_TEXT + rewards.LIKE * rewards.LIKE_FROM,
        )
        self.assertEqual(rewards.at_stake(review, self.other), 0)

    def test_what_was_taken_once_is_not_taken_again(self):
        """Считаем по журналу: начисленное минус уже забранное."""
        review = self.review()
        self.sync()
        reclaim(self.user, rewards.REVIEW_TEXT, BalanceLog.Reason.REVIEW, key=str(review.pk))

        self.assertEqual(rewards.at_stake(review, self.user), 0)

    def test_the_message_names_the_sum_in_proper_russian(self):
        self.assertEqual(rewards.taken_note(0), "")
        for taken, said in ((1, " 1 токен "), (22, " 22 токена "), (25, " 25 токенов "), (11, " 11 токенов ")):
            self.assertIn(said, rewards.taken_note(taken))


class DeleteOnTheSiteTests(TestCase):
    """Те же правила через сами ручки удаления: кто удаляет, тот и определяет, будет ли возврат."""

    @classmethod
    def setUpTestData(cls):
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.teacher = Teacher.objects.create(name="Пётр", surname="Петров")
        cls.author = make_user("author@t.local")
        cls.moderator = make_user("moderator@t.local")
        cls.moderator.user_permissions.add(*Permission.objects.filter(codename__in=[
            "delete_review", "change_comment", "change_material", "change_book", "change_playlist",
        ]))

    def setUp(self):
        self.material = Material.objects.create(
            title="Конспект", subject=self.subject, uploader=self.author, status=Material.Status.APPROVED,
        )
        self.book = Book.objects.create(title="Зорич", uploader=self.author, status=Book.Status.APPROVED)
        self.course = Playlist.objects.create(
            title="Матан", subject=self.subject, uploader=self.author, status=Playlist.Status.APPROVED,
        )
        self.review = Review.objects.create(teacher=self.teacher, author=self.author, text="подробно")
        self.comment = Comment.objects.create(material=self.material, author=self.author, text="разбор")
        self.comment.liked_users.add(
            *[make_user(f"fan{number}@t.local") for number in range(rewards.LIKE_FROM)]
        )
        rewards.sync(self.author)
        self.before = wallet_of(self.author).balance

    def delete(self, who, name, thing):
        self.client.force_login(who)
        with mock.patch("materials.views.notify"), mock.patch("library.views.notify"), \
                mock.patch("lectorium.views.notify"):
            return self.client.post(reverse(name, args=[thing.pk]))

    def said(self, response):
        return " ".join(str(message) for message in get_messages(response.wsgi_request))

    def lost(self):
        return self.before - wallet_of(self.author).balance

    def test_the_author_pays_back_and_is_told_so(self):
        cases = (
            ("review_delete", self.review, rewards.REVIEW_TEXT),
            ("book_delete", self.book, rewards.BOOK),
            ("playlist_delete", self.course, rewards.PLAYLIST),
            ("material_delete", self.material, rewards.MATERIAL),
        )
        for name, thing, price in cases:
            with self.subTest(name=name):
                self.before = wallet_of(self.author).balance

                response = self.delete(self.author, name, thing)

                self.assertFalse(type(thing).objects.filter(pk=thing.pk).exists())
                self.assertEqual(self.lost(), price)
                self.assertIn(f"Списано {price} ", self.said(response))

    def test_a_moderator_deleting_it_costs_the_author_nothing(self):
        cases = (
            ("review_delete", self.review), ("comment_delete", self.comment), ("book_delete", self.book),
            ("playlist_delete", self.course), ("material_delete", self.material),
        )
        for name, thing in cases:
            with self.subTest(name=name):
                response = self.delete(self.moderator, name, thing)

                self.assertFalse(type(thing).objects.filter(pk=thing.pk).exists())
                self.assertEqual(self.lost(), 0)
                self.assertNotIn("Списано", self.said(response))
                self.assertNotIn("HX-Trigger", response.headers)

    def test_a_comment_says_it_right_on_the_page(self):
        """Лента приходит куском, страница не перезагружается — обычное сообщение всплыло
        бы только на следующей. Поэтому тут оно едет событием для всплывающей плашки."""
        response = self.delete(self.author, "comment_delete", self.comment)

        self.assertEqual(self.lost(), rewards.LIKE * rewards.LIKE_FROM)
        notice = json.loads(response.headers["HX-Trigger"])["toast"]
        self.assertIn(f"Списано {rewards.LIKE * rewards.LIKE_FROM} токенов", notice["text"])

    def test_the_form_warns_the_author_before_the_button_is_pressed(self):
        pages = (
            ("material_edit", self.material, rewards.MATERIAL), ("book_edit", self.book, rewards.BOOK),
            ("playlist_edit", self.course, rewards.PLAYLIST),
        )
        for name, thing, price in pages:
            with self.subTest(name=name):
                self.client.force_login(self.author)
                self.assertContains(self.client.get(reverse(name, args=[thing.pk])), f"Спишется {price} токенов")

                self.client.force_login(self.moderator)  # ему удаление чужого ничего не стоит
                self.assertNotContains(self.client.get(reverse(name, args=[thing.pk])), "Спишется")

    def test_a_liked_comment_warns_its_author(self):
        """Предупреждаем только там, где есть что терять: без оплаченных лайков удаление
        комментария ничего не стоит."""
        Comment.objects.create(material=self.material, author=self.author, text="без лайков")
        page = reverse("material_detail", args=[self.material.pk])

        self.client.force_login(self.author)
        self.assertContains(self.client.get(page), "Токены за лайки на нём спишутся.", count=1)

        self.client.force_login(self.moderator)
        self.assertNotContains(self.client.get(page), "спишутся")

    def test_the_review_card_warns_its_author_only(self):
        page = reverse("teacher_detail", args=[self.teacher.pk])
        self.client.force_login(self.author)
        self.assertContains(self.client.get(page), "Токены, начисленные за него, спишутся.")

        self.client.force_login(self.moderator)
        self.assertNotContains(self.client.get(page), "спишутся")
