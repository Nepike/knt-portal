from io import BytesIO, StringIO

from django.core.management import call_command
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from django.core.files.uploadedfile import SimpleUploadedFile

from PIL import Image as PilImage

from attachments.models import File
from core.models import Subject
from comments.models import Comment
from lectorium.models import Playlist
from materials.models import Material
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

    def downloaded(self, *counts):
        material = self.material()
        for number, count in enumerate(counts):
            File.objects.create(
                material=material, name=f"f{number}", file=f"f{number}.pdf",
                size=1, uploader=self.user, downloads=count,
            )

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

    def test_a_deleted_course_does_not_take_its_payment_back(self):
        """Выплаченное назад не забирается — иначе «удалить и залить заново» стало бы фермой."""
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
