import random
import re
from collections import Counter
from functools import partial
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import mkdtemp
from unittest import mock

from django.contrib.admin import site
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from PIL import Image as PilImage

from attachments.media import media_url
from economy.models import BalanceLog
from economy.rewards import WELCOME
from economy.services import NotEnoughFunds, credit, wallet_of
# Разбор mp4 и построитель коробок живут в intake: их же берёт с собой пекарня.
from intake.tests import make_mp4
from users.models import User

from . import specs
from .admin import CaseAdmin, CosmeticItemAdmin
from .cases import ODDS, REEL, REEL_ODDS, STOP, AllCollected, CaseClosed, _faces, draw, odds, open_case, reel
from .forms import CaseForm, CosmeticItemForm
from .models import Case, CaseOpening, CosmeticItem, UserItem
from .services import NotOwned, equip, grant, inventory, outfit, unequip, worn
from .shop import AlreadyOwned, NotForSale, buy, on_sale
from .views import PREVIEWS

MANUAL = BalanceLog.Reason.MANUAL

R = CosmeticItem.Rarity


def make_user(email="u@t.local"):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


def make_frame(name="Пламя", rarity=CosmeticItem.Rarity.RARE, kind=CosmeticItem.Kind.AVATAR_FRAME):
    item = CosmeticItem(name=name, rarity=rarity, kind=kind)
    item.image.save(f"{name}.webp", _png(), save=False)
    item.save()
    return item


def _png(size=(8, 8)):
    buffer = BytesIO()
    PilImage.new("RGBA", size, (255, 0, 0, 128)).save(buffer, format="PNG")
    return SimpleUploadedFile("f.png", buffer.getvalue(), content_type="image/png")


def make_apng(path, frames=3):
    """Настоящий APNG на диске: команда переноса читает файлы, а не выдумки."""
    pages = []
    for step in range(frames):
        page = PilImage.new("RGBA", (224, 224), (0, 0, 0, 0))
        page.paste(PilImage.new("RGBA", (20, 224), (255, 0, 0, 255)), (step * 20, 0))
        pages.append(page)
    pages[0].save(path, format="PNG", save_all=True, append_images=pages[1:], duration=80, loop=0)
    return path


class EquipTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.flame = make_frame("Пламя")
        self.neon = make_frame("Неон")

    def test_granting_twice_does_not_double_the_item(self):
        grant(self.user, self.flame)
        grant(self.user, self.flame)

        self.assertEqual(UserItem.objects.filter(user=self.user).count(), 1)

    def test_equipping_takes_the_previous_one_off(self):
        # Слот один: на старом сайте этого правила не было, и надетым оказывался последний.
        equip(self.user, grant(self.user, self.flame).item)
        equip(self.user, grant(self.user, self.neon).item)

        self.assertEqual(worn(self.user), self.neon)
        self.assertEqual(UserItem.objects.filter(user=self.user, equipped=True).count(), 1)

    def test_a_stranger_item_cannot_be_worn(self):
        with self.assertRaises(NotOwned):
            equip(self.user, self.flame)

    def test_the_database_itself_refuses_two_worn_at_once(self):
        # Не только сервис: правило должно держаться, даже если кто-то напишет мимо него.
        first = grant(self.user, self.flame)
        second = grant(self.user, self.neon)
        UserItem.objects.filter(pk=first.pk).update(equipped=True)

        with self.assertRaises(IntegrityError), transaction.atomic():
            UserItem.objects.filter(pk=second.pk).update(equipped=True)

    def test_slots_do_not_take_each_other_off(self):
        # Рамка и шапка — разные слоты: надеть одно не должно снимать другое.
        header = make_frame("Туманность", kind=CosmeticItem.Kind.PROFILE_HEADER)
        equip(self.user, grant(self.user, self.flame).item)

        equip(self.user, grant(self.user, header).item)

        self.assertEqual(worn(self.user), self.flame)
        self.assertEqual(worn(self.user, CosmeticItem.Kind.PROFILE_HEADER), header)

    def test_taking_a_header_off_leaves_the_frame_on(self):
        header = make_frame("Туманность", kind=CosmeticItem.Kind.PROFILE_HEADER)
        equip(self.user, grant(self.user, self.flame).item)
        equip(self.user, grant(self.user, header).item)

        unequip(self.user, CosmeticItem.Kind.PROFILE_HEADER)

        self.assertEqual(worn(self.user), self.flame)
        self.assertIsNone(worn(self.user, CosmeticItem.Kind.PROFILE_HEADER))

    def test_the_inventory_goes_from_plain_to_rare(self):
        # Сортировать по самой редкости нельзя: в базе это строка, и алфавит ставит
        # legendary между epic и mythical.
        grant(self.user, self.flame)  # редкая, из setUp
        for name, rarity in (("Миф", R.MYTHICAL), ("Обычная", R.COMMON), ("Легенда", R.LEGENDARY)):
            grant(self.user, make_frame(name, rarity))

        order = [own.item.rarity for own in inventory(self.user)]

        self.assertEqual(order, [R.COMMON, R.RARE, R.LEGENDARY, R.MYTHICAL])

    def test_nothing_is_worn_by_default(self):
        grant(self.user, self.flame)
        self.assertIsNone(worn(self.user))

    def test_unequip_leaves_the_item_in_the_inventory(self):
        equip(self.user, grant(self.user, self.flame).item)

        unequip(self.user)

        self.assertIsNone(worn(self.user))
        self.assertEqual(UserItem.objects.filter(user=self.user).count(), 1)

    def test_changing_the_slot_of_an_item_moves_it_in_everyones_inventory(self):
        """Вид продублирован в UserItem — правка в админке обязана поехать следом,
        иначе вещь останется в чужом блоке и займёт не тот слот."""
        equip(self.user, grant(self.user, self.flame).item)

        self.flame.kind = CosmeticItem.Kind.PROFILE_HEADER
        self.flame.save()

        owned = UserItem.objects.get(user=self.user, item=self.flame)
        self.assertEqual(owned.kind, CosmeticItem.Kind.PROFILE_HEADER)
        self.assertFalse(owned.equipped)  # в новом слоте могло быть надето своё

    def test_everything_worn_comes_in_one_query(self):
        # Слотов будет больше значков и фонов — запрос на каждый не годится.
        equip(self.user, grant(self.user, self.flame).item)
        equip(self.user, grant(self.user, make_frame("Туманность", kind=CosmeticItem.Kind.PROFILE_HEADER)).item)

        with self.assertNumQueries(1):
            on = outfit(self.user)

        self.assertEqual(set(on), {CosmeticItem.Kind.AVATAR_FRAME, CosmeticItem.Kind.PROFILE_HEADER})


class ProfileTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.other = make_user("o@t.local")
        self.flame = make_frame("Пламя")
        self.client.force_login(self.user)

    def url(self, person=None):
        return reverse("profile", args=[(person or self.user).pk])

    def test_the_worn_frame_shows_on_the_avatar(self):
        equip(self.user, grant(self.user, self.flame).item)

        response = self.client.get(self.url())

        self.assertEqual(response.context["worn"], self.flame)
        # Подпись у картинки рамки — её название; адрес в разметке процентно закодирован.
        self.assertContains(response, f'alt="{self.flame.name}"')

    def test_the_inventory_is_only_yours(self):
        # Чужой сундук — витрина «вот чего у тебя нет», собирать её незачем.
        grant(self.other, self.flame)

        self.assertEqual(list(self.client.get(self.url(self.other)).context["items"]), [])

    def test_equipping_through_the_page(self):
        grant(self.user, self.flame)

        response = self.client.post(reverse("item_equip", args=[self.flame.pk]))

        self.assertRedirects(response, self.url())
        self.assertEqual(worn(self.user), self.flame)

    def test_you_cannot_equip_what_you_do_not_own(self):
        response = self.client.post(reverse("item_equip", args=[self.flame.pk]), follow=True)

        self.assertIsNone(worn(self.user))
        self.assertContains(response, "Такой вещи у тебя нет")

    def test_equipping_is_not_done_by_a_get(self):
        grant(self.user, self.flame)

        self.assertEqual(self.client.get(reverse("item_equip", args=[self.flame.pk])).status_code, 405)
        self.assertIsNone(worn(self.user))

    def test_a_stranger_frame_does_not_land_on_my_avatar_in_the_menu(self):
        """core/_avatar.html включается ТОЛЬКО с `only`: без него аватар в меню аккаунта
        подхватывал `worn` из контекста чужой страницы профиля и надевал чужую рамку."""
        equip(self.other, grant(self.other, self.flame).item)

        response = self.client.get(self.url(self.other))

        # Рамка на странице ровно одна — на аватаре хозяина страницы, а не в меню слева.
        self.assertEqual(response.content.decode().count(f'alt="{self.flame.name}"'), 1)

    def test_my_own_frame_shows_in_the_menu_on_every_page(self):
        # Иначе купленную вещь видно только на своей же странице профиля.
        equip(self.user, grant(self.user, self.flame).item)

        response = self.client.get(reverse("material_list"))

        self.assertEqual(response.context["my_frame"], self.flame)
        self.assertContains(response, f'alt="{self.flame.name}"')

    def test_the_menu_does_not_pay_for_a_frame_on_htmx_fragments(self):
        equip(self.user, grant(self.user, self.flame).item)

        response = self.client.get(reverse("material_list"), headers={"hx-request": "true"})

        self.assertNotIn("my_frame", response.context)


class SpecTests(TestCase):
    """Приёмка по спеке: сайт не конвертирует, он отказывает с внятной причиной.
    Договор целиком — docs/media-pipeline.md."""

    def refusal(self, kind, size):
        form = CosmeticItemForm(
            {"name": "Проба", "kind": kind, "rarity": R.COMMON, "sold": True},
            {"image": _png(size)},
        )
        form.is_valid()
        return " ".join(form.errors.get("__all__", []))

    def test_a_background_of_the_wrong_shape_is_refused_with_the_reason(self):
        problem = self.refusal(CosmeticItem.Kind.PROFILE_BACKGROUND, (1280, 960))

        self.assertIn("16:9", problem)
        self.assertIn("1280×960", problem)

    def test_a_background_of_the_right_shape_passes(self):
        self.assertEqual(self.refusal(CosmeticItem.Kind.PROFILE_BACKGROUND, (1920, 1080)), "")

    def test_a_header_is_measured_by_its_own_shape(self):
        # Та же картинка, что годится в фон, для шапки не годится — и наоборот.
        self.assertIn("6:1", self.refusal(CosmeticItem.Kind.PROFILE_HEADER, (1920, 1080)))
        self.assertEqual(self.refusal(CosmeticItem.Kind.PROFILE_HEADER, (1536, 256)), "")

    def test_too_small_is_refused_even_with_the_right_shape(self):
        self.assertIn("слишком мелко", self.refusal(CosmeticItem.Kind.PROFILE_BACKGROUND, (320, 180)))

    def test_editing_a_saved_item_leaves_its_file_alone(self):
        """Правка названия не должна упираться в то, что рамка со старого сайта
        на пару пикселей не квадратная."""
        item = make_frame("Кривая")
        CosmeticItem.objects.filter(pk=item.pk).update(kind=CosmeticItem.Kind.PROFILE_BACKGROUND)

        form = CosmeticItemForm(
            {"name": "Другое имя", "kind": CosmeticItem.Kind.PROFILE_BACKGROUND,
             "rarity": R.COMMON, "sold": True},
            instance=CosmeticItem.objects.get(pk=item.pk),
        )

        self.assertTrue(form.is_valid(), form.errors)


class VideoSpecTests(TestCase):
    """Приёмка видео у вещи. Сама сверка с рецептом — общая, и проверена в intake.tests;
    здесь то, что знает только косметика: какому виду видео бывает и по какому рецепту."""

    def refusal(self, kind=CosmeticItem.Kind.PROFILE_BACKGROUND, **kwargs):
        return specs.check_video(kind, make_mp4(**kwargs)) or ""

    def test_a_proper_background_passes(self):
        self.assertEqual(self.refusal(), "")

    def test_the_header_is_measured_by_its_own_recipe(self):
        """Фон в слоте шапки не проходит: у каждого вида свой рецепт, а не общий."""
        header = CosmeticItem.Kind.PROFILE_HEADER

        self.assertEqual(self.refusal(header, width=1536, height=256), "")
        self.assertIn("1536×256", self.refusal(header))

    def test_a_reason_from_the_shared_check_reaches_the_item(self):
        """Переходник обязан доносить причину дословно, а не глотать её."""
        self.assertIn("faststart", self.refusal(faststart=False))

    def test_frames_are_not_allowed_to_be_video(self):
        # Нужен прозрачный проём под лицо, а прозрачного видео для всех браузеров нет.
        self.assertIn("только картинка", self.refusal(kind=CosmeticItem.Kind.AVATAR_FRAME, width=224, height=224))

    def test_anything_but_mp4_is_refused(self):
        self.assertIn("mp4", self.refusal(name="fon.webm"))

    def test_the_form_refuses_a_bad_video_with_the_reason(self):
        form = CosmeticItemForm(
            {"name": "Проба", "kind": CosmeticItem.Kind.PROFILE_BACKGROUND, "rarity": R.COMMON, "sold": True},
            {"image": _png((1920, 1080)), "video": make_mp4(audio=True)},
        )

        self.assertFalse(form.is_valid())
        self.assertIn("вуковая дорожка", " ".join(form.errors["__all__"]))


class ArtTests(TestCase):
    """Одна вещь рисуется одинаково везде: есть видео — <video>, нет — <img>."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def make_live_background(self):
        item = make_frame("Живой фон", R.RARE, CosmeticItem.Kind.PROFILE_BACKGROUND)
        item.video.save("fon.mp4", make_mp4(), save=True)
        return item

    def test_a_still_thing_is_drawn_as_a_picture(self):
        item = make_frame("Тихий фон", R.RARE, CosmeticItem.Kind.PROFILE_BACKGROUND)
        equip(self.user, grant(self.user, item).item)

        page = self.client.get(reverse("profile", args=[self.user.pk])).content.decode()

        self.assertIn(f'src="{media_url(item.image)}"', page)
        self.assertNotIn("<video", page)

    def test_a_live_thing_is_drawn_as_video_with_a_poster(self):
        item = self.make_live_background()
        equip(self.user, grant(self.user, item).item)

        page = self.client.get(reverse("profile", args=[self.user.pk])).content.decode()

        self.assertIn(f'src="{media_url(item.video)}"', page)
        self.assertIn(f'poster="{media_url(item.image)}"', page)
        self.assertIn("playsinline", page)  # без него iOS откроет фон на весь экран

    def test_on_a_shelf_neither_the_video_nor_its_poster_loads_until_seen(self):
        """Safari на телефоне держит считанные декодеры разом, а постеры двадцати фонов —
        это ещё три мегабайта. Видео на полке играет одно, его выбирает сама полка."""
        item = self.make_live_background()

        shelves = self.client.get(reverse("shop")).content.decode().split('id="shelves"')[1]

        self.assertIn('x-data="lazyMedia"', shelves)
        self.assertIn(f'data-src="{media_url(item.video)}"', shelves)
        self.assertIn(f'data-poster="{media_url(item.image)}"', shelves)
        self.assertNotIn(" poster=", shelves)
        self.assertNotIn("autoplay", shelves)

    def test_in_the_inventory_the_video_still_waits_for_the_tile(self):
        item = self.make_live_background()
        grant(self.user, item)

        page = self.client.get(reverse("profile", args=[self.user.pk])).content.decode()

        self.assertIn('x-data="lazyVideo"', page)
        self.assertIn(f'poster="{media_url(item.image)}"', page)


class PriceTests(TestCase):
    """Цена идёт от вида и ступени, своя — исключение."""

    def test_the_kind_and_the_rarity_set_the_price(self):
        self.assertEqual(make_frame("Обычная", R.COMMON).cost, 100)
        self.assertEqual(make_frame("Мифическая", R.MYTHICAL).cost, 4000)
        self.assertEqual(make_frame("Фон", R.COMMON, CosmeticItem.Kind.PROFILE_BACKGROUND).cost, 150)
        self.assertEqual(make_frame("Шапка", R.MYTHICAL, CosmeticItem.Kind.PROFILE_HEADER).cost, 2500)

    def test_every_kind_has_a_price_for_every_rarity(self):
        # Без строки вещь нового вида стоила бы как самая дорогая — не поломка, но и не цена.
        for kind in CosmeticItem.Kind:
            for rarity in R:
                with self.subTest(kind=kind, rarity=rarity):
                    self.assertIn(rarity, CosmeticItem.PRICES.get(kind, {}))

    def test_a_background_costs_more_than_a_frame_and_a_header_less(self):
        """Фон кроет всю страницу профиля, шапка — узкая полоса под именем."""
        prices, kind = CosmeticItem.PRICES, CosmeticItem.Kind
        for rarity in R:
            with self.subTest(rarity=rarity):
                self.assertLess(prices[kind.PROFILE_HEADER][rarity], prices[kind.AVATAR_FRAME][rarity])
                self.assertLess(prices[kind.AVATAR_FRAME][rarity], prices[kind.PROFILE_BACKGROUND][rarity])

    def test_the_welcome_money_buys_more_than_one_thing(self):
        # Решение пользователя 18 окт 2026: «со стартовых только одна рамка — неправильно».
        self.assertGreaterEqual(WELCOME, 2 * CosmeticItem.PRICES[CosmeticItem.Kind.AVATAR_FRAME][R.RARE])

    def test_an_own_price_wins(self):
        item = make_frame("Особая", R.COMMON)
        item.price = 9000
        self.assertEqual(item.cost, 9000)

    def test_free_is_a_price_too(self):
        # 0 — это не «пусто»: подарок в витрине должен уметь стоить ноль.
        item = make_frame("Даром", R.EPIC)
        item.price = 0
        self.assertEqual(item.cost, 0)

    def test_an_unknown_rarity_or_kind_costs_the_most(self):
        # Ошибиться в сторону «дорого» не страшно, в сторону «даром» — раздать полбакета.
        item = make_frame("Кривая", R.COMMON)
        CosmeticItem.objects.filter(pk=item.pk).update(rarity="неведомая")
        self.assertEqual(CosmeticItem.objects.get(pk=item.pk).cost, 6000)

        CosmeticItem.objects.filter(pk=item.pk).update(rarity=R.COMMON, kind="значок")
        self.assertEqual(CosmeticItem.objects.get(pk=item.pk).cost, 6000)


class BuyTests(TestCase):
    """Покупка: деньги и вещь меняются местами целиком или никак."""

    def setUp(self):
        self.user = make_user()
        self.flame = make_frame("Пламя", R.RARE)  # 250

    def test_buying_takes_the_money_and_gives_the_thing(self):
        credit(self.user, 1000, MANUAL)

        buy(self.user, self.flame)

        self.assertEqual(wallet_of(self.user).balance, 750)
        self.assertTrue(UserItem.objects.filter(user=self.user, item=self.flame).exists())

    def test_the_purchase_says_in_the_journal_what_was_bought(self):
        credit(self.user, 1000, MANUAL)

        buy(self.user, self.flame)

        entry = BalanceLog.objects.filter(reason=BalanceLog.Reason.PURCHASE).get()
        self.assertEqual(entry.amount, -250)
        self.assertEqual(entry.key, f"item:{self.flame.pk}")
        self.assertEqual(entry.note, "Пламя")

    def test_bought_things_are_not_put_on_by_themselves(self):
        # Решение пользователя: покупают и про запас, подмена надетого читалась бы поломкой.
        credit(self.user, 1000, MANUAL)

        buy(self.user, self.flame)

        self.assertIsNone(worn(self.user))

    def test_short_of_money_changes_nothing(self):
        credit(self.user, 100, MANUAL)

        with self.assertRaises(NotEnoughFunds):
            buy(self.user, self.flame)

        self.assertEqual(wallet_of(self.user).balance, 100)
        self.assertFalse(UserItem.objects.filter(user=self.user).exists())

    def test_the_same_thing_is_not_sold_twice(self):
        credit(self.user, 2000, MANUAL)
        buy(self.user, self.flame)

        with self.assertRaises(AlreadyOwned):
            buy(self.user, self.flame)

        self.assertEqual(wallet_of(self.user).balance, 1750)

    def test_a_thing_out_of_the_shop_cannot_be_bought(self):
        # Вещи для кейсов и выдачи руками в витрине не лежат и покупаться не должны.
        self.flame.sold = False
        self.flame.save()
        credit(self.user, 2000, MANUAL)

        with self.assertRaises(NotForSale):
            buy(self.user, self.flame)

        self.assertEqual(wallet_of(self.user).balance, 2000)

    def test_the_shop_shows_only_what_is_for_sale(self):
        hidden = make_frame("Тайная", R.EPIC)
        hidden.sold = False
        hidden.save()

        self.assertEqual(list(on_sale()), [self.flame])


class ShopPageTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.cheap = make_frame("Дешёвая", R.COMMON)     # 100
        self.dear = make_frame("Дорогая", R.MYTHICAL)    # 4000
        self.client.force_login(self.user)               # вход начисляет стартовые 500

    def on_shelves(self, response):
        return {item.name: item for _, _, items in response.context["shelves"] for item in items}

    def test_the_page_shows_prices_and_what_is_out_of_reach(self):
        response = self.client.get(reverse("shop"))

        by_name = self.on_shelves(response)
        self.assertEqual(response.context["coins"], 500)
        self.assertEqual(by_name["Дешёвая"].short, 0)
        self.assertEqual(by_name["Дорогая"].short, 3500)

    def test_buying_through_the_page(self):
        response = self.client.post(reverse("item_buy", args=[self.cheap.pk]), follow=True)

        self.assertContains(response, "Куплено: Дешёвая")
        self.assertEqual(wallet_of(self.user).balance, 400)

    def test_what_is_already_owned_is_marked_instead_of_offered(self):
        self.client.post(reverse("item_buy", args=[self.cheap.pk]))

        response = self.client.get(reverse("shop"))

        by_name = self.on_shelves(response)
        self.assertTrue(by_name["Дешёвая"].owned)
        self.assertContains(response, "в инвентаре")

    def test_a_purchase_out_of_reach_is_refused_with_words(self):
        response = self.client.post(reverse("item_buy", args=[self.dear.pk]), follow=True)

        self.assertContains(response, "Не хватает токенов")
        self.assertEqual(wallet_of(self.user).balance, 500)

    def test_buying_is_not_done_by_a_get(self):
        # Иначе хватило бы заманить человека на картинку по этому адресу.
        self.assertEqual(self.client.get(reverse("item_buy", args=[self.cheap.pk])).status_code, 405)
        self.assertEqual(wallet_of(self.user).balance, 500)

    def test_a_shelf_shows_animations_but_only_once_they_are_seen(self):
        """Витрина показывает анимации, а не обложки — ассортимент смотрят глазами, а на
        телефоне наведения нет вовсе. Но браузерный loading=lazy качал с запасом в два
        экрана, то есть все 26 МБ рамок разом: источник ставит скрипт, когда плитку видно."""
        page = self.client.get(reverse("shop")).content.decode()
        url = re.escape(media_url(self.cheap.image))

        self.assertRegex(page, rf'x-data="lazyMedia" data-src="{url}"')
        self.assertNotRegex(page, rf'(?<!data-)src="{url}"')

class ShowcaseTests(TestCase):
    """Витрина полками: порядок, «мне по карману», листание в окне товара."""

    def setUp(self):
        self.user = make_user()
        self.common = make_frame("Обычная", R.COMMON)          # 100
        self.rare = make_frame("Редкая", R.RARE)               # 250
        self.myth = make_frame("Мифическая", R.MYTHICAL)       # 4000
        self.back = make_frame("Фон", R.EPIC, CosmeticItem.Kind.PROFILE_BACKGROUND)  # 900
        self.head = make_frame("Шапка", R.COMMON, CosmeticItem.Kind.PROFILE_HEADER)  # 50
        self.client.force_login(self.user)  # стартовые 500

    def shelves(self, **query):
        response = self.client.get(reverse("shop"), query)
        return {kind: [item.name for item in items] for kind, _, items in response.context["shelves"]}

    def test_frames_backgrounds_headers_and_the_rarest_first(self):
        response = self.client.get(reverse("shop"))

        self.assertEqual([(kind, title) for kind, title, _ in response.context["shelves"]], [
            ("avatar_frame", "Рамки аватара"), ("profile_background", "Фоны профиля"), ("profile_header", "Шапки профиля"),
        ])
        self.assertEqual(self.shelves()["avatar_frame"], ["Мифическая", "Редкая", "Обычная"])

    def test_the_cheapest_first_on_request_and_nonsense_is_the_default(self):
        self.assertEqual(self.shelves(order="cheap")["avatar_frame"], ["Обычная", "Редкая", "Мифическая"])
        self.assertEqual(self.shelves(order="<script>")["avatar_frame"], ["Мифическая", "Редкая", "Обычная"])

    def test_affordable_hides_what_is_too_dear_and_what_is_owned(self):
        grant(self.user, self.common)

        response = self.client.get(reverse("shop"), {"afford": "1"})

        # Обычная уже есть, мифическая не по карману, фон тоже; редкая и шапка — в самый раз.
        self.assertEqual(self.shelves(afford="1"), {"avatar_frame": ["Редкая"], "profile_background": [], "profile_header": ["Шапка"]})
        # Полка не исчезает: видно, что фоны в магазине есть, просто пока не по карману.
        self.assertContains(response, "Фоны профиля")
        self.assertContains(response, "Пока ничего не по карману")
        # Окно товара с полки знает подбор — иначе стрелки в нём листали бы всю полку.
        shelves = response.content.decode().split('id="shelves"')[1]
        self.assertIn(f'hx-get="{reverse("item_card", args=[self.head.pk])}?afford=1"', shelves)

    def test_htmx_gets_only_the_shelves_and_the_address_to_share(self):
        response = self.client.get(reverse("shop"), {"order": "cheap", "afford": "1"}, headers={"HX-Request": "true"})

        self.assertTemplateUsed(response, "cosmetics/_shelves.html")
        self.assertTemplateNotUsed(response, "cosmetics/shop.html")
        self.assertEqual(response["HX-Push-Url"], f"{reverse('shop')}?order=cheap&afford=1")

        plain = self.client.get(reverse("shop"), {"order": "rare"}, headers={"HX-Request": "true"})
        self.assertEqual(plain["HX-Push-Url"], reverse("shop"))  # умолчания в адрес не идут

    def card(self, item, **query):
        return self.client.get(reverse("item_card", args=[item.pk]), query)

    def test_the_card_leafs_through_its_own_shelf(self):
        response = self.card(self.rare)

        self.assertEqual((response.context["position"], response.context["total"]), (2, 3))
        self.assertEqual(response.context["before"], self.myth)
        self.assertEqual(response.context["after"], self.common)
        self.assertContains(response, "2 из 3")
        self.assertContains(response, f'hx-get="{reverse("item_card", args=[self.myth.pk])}"')

    def test_the_card_keeps_the_order_and_the_tick(self):
        credit(self.user, 1000, MANUAL)  # 1500: обычная и редкая по карману, мифическая нет

        response = self.card(self.rare, order="cheap", afford="1")

        self.assertEqual((response.context["position"], response.context["total"]), (2, 2))
        self.assertIsNone(response.context["after"])
        self.assertContains(response, f'hx-get="{reverse("item_card", args=[self.common.pk])}?order=cheap&amp;afford=1"')
        # И покупка вернёт в витрину с тем же подбором.
        self.assertContains(response, '<input type="hidden" name="order" value="cheap">')
        self.assertContains(response, '<input type="hidden" name="afford" value="1">')

    def test_a_thing_outside_the_picked_shelf_leafs_through_the_whole_one(self):
        """Пока окно открыто, вещь могла уйти с отобранной полки — её купили или потратили
        деньги в другой вкладке. Окно тогда листает полку целиком, а не падает."""
        response = self.card(self.myth, afford="1")

        self.assertEqual((response.context["position"], response.context["total"]), (1, 3))

    def test_after_buying_the_shop_comes_back_as_it_was_left(self):
        response = self.client.post(reverse("item_buy", args=[self.common.pk]), {"order": "cheap", "afford": "1"})
        self.assertRedirects(response, f"{reverse('shop')}?order=cheap&afford=1", fetch_redirect_response=False)

        response = self.client.post(reverse("item_buy", args=[self.head.pk]), {"order": "//evil.example"})
        self.assertRedirects(response, reverse("shop"), fetch_redirect_response=False)


class OfferTests(TestCase):
    """Карточка товара: предпросмотр на себе, цена и кнопка."""

    def setUp(self):
        self.user = make_user()
        self.frame = make_frame("Пламя", R.COMMON)                                   # 100
        self.header = make_frame("Ночь", R.MYTHICAL, CosmeticItem.Kind.PROFILE_HEADER)  # 2500
        self.client.force_login(self.user)                                            # стартовые 500

    def card(self, item):
        return self.client.get(reverse("item_card", args=[item.pk]))

    def test_an_affordable_thing_offers_a_button(self):
        response = self.card(self.frame)

        self.assertContains(response, "Купить за 100")
        self.assertEqual(response.context["short"], 0)

    def test_an_expensive_thing_says_how_much_is_missing(self):
        response = self.card(self.header)

        self.assertEqual(response.context["short"], 2000)
        self.assertContains(response, "Не хватает 2000")
        self.assertNotContains(response, "Купить за")

    def test_what_is_owned_is_not_sold_again(self):
        grant(self.user, self.frame)

        response = self.card(self.frame)

        self.assertContains(response, "Уже в инвентаре")
        self.assertNotContains(response, "Купить за")

    def test_the_preview_puts_the_frame_on_the_buyer(self):
        # Предпросмотр рисуется настоящим core/_avatar.html, иначе показывал бы не то.
        response = self.card(self.frame)

        self.assertContains(response, f'alt="{self.frame.name}"')

    def test_a_thing_out_of_the_shop_has_no_card(self):
        self.frame.sold = False
        self.frame.save()

        self.assertEqual(self.card(self.frame).status_code, 404)

    def test_each_kind_gets_its_own_preview(self):
        """Рамку и шапку надо видеть вблизи, фон — картой всей страницы. Один общий
        предпросмотр обслуживал бы кого-то плохо."""
        background = make_frame("Бездна", R.COMMON, CosmeticItem.Kind.PROFILE_BACKGROUND)

        self.assertTemplateUsed(self.card(self.frame), "cosmetics/preview/card.html")
        self.assertTemplateUsed(self.card(self.header), "cosmetics/preview/card.html")
        self.assertTemplateUsed(self.card(background), "cosmetics/preview/page.html")

    def test_every_kind_on_sale_has_a_preview_of_its_own(self):
        """Новый вид вещи без своей строки в PREVIEWS молча уехал бы в чужой шаблон."""
        self.assertEqual(set(PREVIEWS), set(CosmeticItem.Kind.values))


class ImportTests(TestCase):
    """Перенос рамок: чтение файлов и превращение APNG в лёгкую пару «анимация + обложка»."""

    def setUp(self):
        self.folder = Path(mkdtemp())
        make_apng(self.folder / "aaa.png")

    def run_it(self, *args):
        out = StringIO()
        call_command("import_frames", "--more", str(self.folder), *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        output = self.run_it()

        self.assertEqual(CosmeticItem.objects.count(), 0)
        self.assertIn("Пробный прогон", output)

    def test_apply_creates_an_item_with_its_picture(self):
        self.run_it("--apply")

        item = CosmeticItem.objects.get()
        self.assertEqual(item.name, "Рамка 1")
        self.assertEqual(item.rarity, CosmeticItem.Rarity.COMMON)
        self.assertTrue(item.image.name)

    def test_the_animation_is_carried_over_untouched(self):
        # Пережатие пробовали дважды и оба раза отказались: WebP лоссИ, на пиксельных
        # рамках это видно, а экономит всего 38%.
        self.run_it("--apply")

        with PilImage.open(CosmeticItem.objects.get().image) as picture:
            self.assertEqual(picture.n_frames, 3)
            self.assertEqual(picture.format, "PNG")

    def test_running_it_again_adds_nothing(self):
        self.run_it("--apply")
        self.run_it("--apply")

        self.assertEqual(CosmeticItem.objects.count(), 1)


class AdminFormTests(TestCase):
    """Админка — единственная дверь для анимированных вещей: витрину и профиль они
    получают уже принятыми. Дверь эта была закрыта — поля `video` в наборе не было.

    Правки идут через саму страницу админки: она сохраняет форму с commit=False, и тесты,
    звавшие form.save() напрямую, были зелёными при неработающей уборке файлов.
    """

    def setUp(self):
        self.client.force_login(User.objects.create_superuser(
            email="boss@t.local", name="Иван", surname="Иванов", password="pass12345",
        ))

    def edit(self, item, **changed):
        fields = {"name": item.name, "kind": item.kind, "rarity": item.rarity, "note": "", "sold": "on", "price": ""}
        # Уборка прежних файлов ждёт коммита, а тест весь живёт в одной транзакции.
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(
                reverse("admin:cosmetics_cosmeticitem_change", args=[item.pk]), {**fields, **changed},
            )
        self.assertEqual(answer.status_code, 302, answer.content.decode()[:2000] if answer.status_code == 200 else "")
        return CosmeticItem.objects.get(pk=item.pk)

    def test_the_video_field_is_reachable(self):
        self.assertIn("video", CosmeticItemAdmin(CosmeticItem, site).get_fields(None))

    def test_a_replaced_file_does_not_stay_in_the_bucket(self):
        """Блоб снимается вместе с записью (post_delete в attachments/storage.py),
        но при ЗАМЕНЕ ссылка на прежний пропадала, а сам он оставался сиротой."""
        item = make_frame("Замена")
        was = item.image.name

        now = self.edit(item, image=_png((224, 224)))

        self.assertNotEqual(now.image.name, was)
        self.assertFalse(item.image.storage.exists(was))
        self.assertTrue(now.image.storage.exists(now.image.name))

    def test_an_untouched_file_survives_an_edit(self):
        item = make_frame("Тихая")
        was = item.image.name

        now = self.edit(item, name="Новое имя")

        self.assertEqual((now.name, now.image.name), ("Новое имя", was))
        self.assertTrue(item.image.storage.exists(was))

    def test_a_cleared_video_does_not_stay_in_the_bucket(self):
        item = make_frame("Была живой", R.RARE, CosmeticItem.Kind.PROFILE_BACKGROUND)
        item.video.save("fon.mp4", make_mp4(), save=True)
        was = item.video.name

        now = self.edit(item, **{"video-clear": "on"})

        self.assertFalse(now.video)
        self.assertFalse(item.video.storage.exists(was))

    def test_the_old_file_stays_while_the_edit_is_not_committed(self):
        # Страница админки — одна транзакция: при откате запись останется со старым файлом.
        item = make_frame("Замена")
        was = item.image.name

        self.client.post(reverse("admin:cosmetics_cosmeticitem_change", args=[item.pk]), {
            "name": item.name, "kind": item.kind, "rarity": item.rarity, "note": "", "sold": "on", "price": "",
            "image": _png((224, 224)),
        })

        self.assertTrue(item.image.storage.exists(was))

    def test_the_tick_in_the_list_still_saves(self):
        # У формы списка своего учёта прежних файлов нет — save_model не должен на него рассчитывать.
        item = make_frame("Витрина")

        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(reverse("admin:cosmetics_cosmeticitem_changelist"), {
                "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1", "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000", "form-0-id": str(item.pk), "_save": "Сохранить",
            })

        self.assertEqual(answer.status_code, 302)
        self.assertFalse(CosmeticItem.objects.get(pk=item.pk).sold)
        self.assertTrue(item.image.storage.exists(item.image.name))


class UnequipTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_a_bogus_slot_takes_nothing_off(self):
        """Подставлять вместо неизвестного вида рамку нельзя: человек снял бы не то."""
        item = make_frame("Пламя")
        equip(self.user, grant(self.user, item).item)

        self.client.post(reverse("item_unequip"), {"kind": "кто-то подставил"})

        self.assertEqual(worn(self.user), item)

    def test_taking_off_an_empty_slot_does_not_claim_it_did_something(self):
        response = self.client.post(reverse("item_unequip"), {"kind": CosmeticItem.Kind.PROFILE_HEADER}, follow=True)

        self.assertNotIn("Снято", [str(m) for m in response.context["messages"]])


class TileTests(TestCase):
    """Надетое видно ярлыком по низу плитки — цветом своей ступени. Ни кольца (спорило
    с рамкой ступени), ни заливки (глушила саму вещь)."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_the_worn_thing_carries_a_tag_and_a_check_and_the_rest_do_not(self):
        on = make_frame("Надетая", R.EPIC)
        off = make_frame("Лежит", R.EPIC)
        equip(self.user, grant(self.user, on).item)
        grant(self.user, off)

        page = self.client.get(reverse("profile", args=[self.user.pk])).content.decode()

        self.assertEqual(page.count("rarity-tag"), 1)
        self.assertEqual(page.count("rarity-check"), 1)
        self.assertNotIn("rarity-on", page)  # прежняя заливка убрана целиком

    def test_an_empty_inventory_points_at_the_shop(self):
        """Единственное место, откуда человеку и правда некуда деться, кроме магазина."""
        page = self.client.get(reverse("profile", args=[self.user.pk])).content.decode()

        self.assertIn(reverse("shop"), page)
        self.assertIn("В магазин", page)


def make_case(name="Рамки", price=300, items=(), active=True):
    case = Case.objects.create(name=name, price=price, active=active)
    case.items.set(items)
    return case


def loose(name, rarity, pk):
    """Вещь без базы — для чистого жребия."""
    return CosmeticItem(pk=pk, name=name, rarity=rarity)


class Rigged:
    """Жребий по заказу: ступень — заданная, вещь внутри неё — первая по имени."""

    def __init__(self, rarity):
        self.rarity = rarity

    def choices(self, population, weights=None, k=1):
        assert self.rarity in population, "заказанной ступени среди возможных нет"
        return [self.rarity] * k

    def choice(self, seq):
        return min(seq, key=lambda item: item.name)

    def sample(self, population, k):
        return list(population)[:k]


class CaseOddsTests(SimpleTestCase):
    """Жребий: ступень по шансам кейса, внутри неё — только новое."""

    def setUp(self):
        self.items = [
            loose("Обычная", R.COMMON, 1), loose("Ещё обычная", R.COMMON, 2), loose("Редкая", R.RARE, 3),
            loose("Эпическая", R.EPIC, 4), loose("Легендарная", R.LEGENDARY, 5), loose("Мифическая", R.MYTHICAL, 6),
        ]
        self.luck = random.Random(18)

    def test_the_odds_are_the_agreed_ones(self):
        # Решение пользователя 18 окт 2026.
        self.assertEqual(ODDS, {R.COMMON: 55, R.RARE: 25, R.EPIC: 12, R.LEGENDARY: 6, R.MYTHICAL: 2})
        self.assertEqual(odds(self.items), [
            (R.COMMON, 0.55), (R.RARE, 0.25), (R.EPIC, 0.12), (R.LEGENDARY, 0.06), (R.MYTHICAL, 0.02),
        ])

    def test_a_rarity_missing_from_the_case_gives_its_share_to_the_rest(self):
        two = [loose("Обычная", R.COMMON, 1), loose("Редкая", R.RARE, 2)]

        self.assertEqual(odds(two), [(R.COMMON, 55 / 80), (R.RARE, 25 / 80)])

    def test_draws_follow_the_odds(self):
        rolls = Counter(draw(self.items, set(), luck=self.luck)[0] for _ in range(20000))

        for rarity, share in ODDS.items():
            with self.subTest(rarity=rarity):
                self.assertAlmostEqual(rolls[rarity] / 20000, share / 100, delta=0.01)

    def test_what_you_own_does_not_move_the_odds(self):
        """Скупил в магазине все обычные — шанс на редкое от этого не растёт: выпавшая
        обычная просто пуста (за неё вернут половину цены). Иначе кейс приносил бы в
        среднем втрое больше своей цены."""
        owned = {item.pk for item in self.items if item.rarity == R.COMMON}

        rolls = [draw(self.items, owned, luck=self.luck) for _ in range(20000)]

        commons = [item for rarity, item in rolls if rarity == R.COMMON]
        self.assertAlmostEqual(len(commons) / 20000, 0.55, delta=0.01)
        self.assertEqual(set(commons), {None})

    def test_inside_a_rarity_only_new_things_drop(self):
        first, second = self.items[:2]

        dropped = {draw([first, second], {first.pk}, luck=self.luck)[1] for _ in range(50)}

        self.assertEqual(dropped, {second})

    def test_a_gift_never_comes_out_empty(self):
        owned = {item.pk for item in self.items if item.rarity != R.MYTHICAL}

        dropped = {draw(self.items, owned, gift=True, luck=self.luck)[1] for _ in range(200)}

        self.assertEqual(dropped, {self.items[-1]})


class ReelTests(SimpleTestCase):
    """Лента рулетки: разнообразная и без близнецов рядом. На исход она не влияет."""

    def setUp(self):
        # Состав как у «Рамок» на сайте: обычных мало, остальных много.
        counts = {R.COMMON: 4, R.RARE: 9, R.EPIC: 6, R.LEGENDARY: 6, R.MYTHICAL: 4}
        self.items = [loose(f"{rarity} {n}", rarity, pk) for pk, (rarity, n) in enumerate(
            ((rarity, n) for rarity, count in counts.items() for n in range(count)), start=1,
        )]
        self.luck = random.Random(18)

    def reels(self, times=300):
        for _ in range(times):
            prize = self.luck.choice(self.items)
            opening = CaseOpening(item=prize, rarity=prize.rarity)
            yield prize, reel(self.items, opening, luck=self.luck)

    def test_the_reel_odds_only_tease(self):
        """Решение пользователя 18 окт 2026: легендарных и мифических в ленте чуть больше,
        «просто чтобы их побольше видеть». Выпадают они по-прежнему по ODDS."""
        self.assertEqual(REEL_ODDS, {R.COMMON: 50, R.RARE: 25, R.EPIC: 12, R.LEGENDARY: 9, R.MYTHICAL: 4})

    def test_the_prize_stands_at_the_stop_and_no_twins_stand_side_by_side(self):
        for prize, tiles in self.reels():
            self.assertEqual((len(tiles), tiles[STOP]), (REEL, prize))
            self.assertFalse([n for n in range(REEL - 1) if tiles[n].pk == tiles[n + 1].pk])

    def test_one_thing_does_not_flash_every_other_tile(self):
        """Обычных вещей четыре, и их доля делится на всех по кругу, а не достаётся одной."""
        worst = max(Counter(item.pk for item in tiles).most_common(1)[0][1] for _, tiles in self.reels())

        self.assertLessEqual(worst, 9)

    def test_the_reel_shows_every_rarity_of_the_case_and_at_most_ten_things(self):
        for prize, tiles in self.reels(50):
            self.assertLessEqual(len({item.pk for item in tiles}), 10)
        faces = _faces(self.items, self.items[0], self.luck)
        self.assertEqual(set(faces), set(R))

    def test_rarities_on_the_reel_follow_the_reel_odds(self):
        shares = Counter(item.rarity for _, tiles in self.reels(1000) for item in tiles)
        total = sum(shares.values())

        for rarity, share in REEL_ODDS.items():
            with self.subTest(rarity=rarity):
                self.assertAlmostEqual(shares[rarity] / total, share / 100, delta=0.02)


class CaseOpenTests(TestCase):
    """Открытие: деньги, вещь и запись в журнале — целиком или никак."""

    def setUp(self):
        self.user = make_user()
        self.common = make_frame("Обычная", R.COMMON)
        self.other = make_frame("Другая", R.COMMON)
        self.rare = make_frame("Редкая", R.RARE)
        self.case = make_case("Рамки", 300, [self.common, self.other, self.rare])

    def gift_is_used(self):
        CaseOpening.objects.create(user=self.user, case=self.case, rarity=R.COMMON, paid=0)

    def test_the_first_case_is_a_gift_even_with_an_empty_wallet(self):
        opening = open_case(self.user, self.case, luck=Rigged(R.RARE))

        self.assertEqual((opening.paid, opening.item), (0, self.rare))
        self.assertTrue(UserItem.objects.filter(user=self.user, item=self.rare).exists())
        self.assertEqual(wallet_of(self.user).balance, 0)
        self.assertFalse(BalanceLog.objects.filter(reason=BalanceLog.Reason.CASE).exists())

    def test_after_the_gift_a_case_costs_its_price(self):
        self.gift_is_used()
        credit(self.user, 1000, MANUAL)

        opening = open_case(self.user, self.case, luck=Rigged(R.RARE))

        self.assertEqual((opening.paid, opening.item), (300, self.rare))
        self.assertEqual(wallet_of(self.user).balance, 700)
        entry = BalanceLog.objects.get(reason=BalanceLog.Reason.CASE)
        self.assertEqual((entry.amount, entry.key), (-300, f"case:{self.case.pk}"))
        self.assertIn("Редкая", entry.note)  # в истории кошелька видно, что выпало

    def test_the_gift_is_once_per_person_not_per_case(self):
        another = make_case("Ещё", 400, [self.rare])
        open_case(self.user, self.case, luck=Rigged(R.COMMON))
        credit(self.user, 1000, MANUAL)

        open_case(self.user, another, luck=Rigged(R.RARE))

        self.assertEqual(wallet_of(self.user).balance, 600)

    def test_short_of_money_changes_nothing(self):
        self.gift_is_used()
        credit(self.user, 299, MANUAL)

        with self.assertRaises(NotEnoughFunds):
            open_case(self.user, self.case)

        self.assertEqual(wallet_of(self.user).balance, 299)
        self.assertEqual(CaseOpening.objects.filter(user=self.user).count(), 1)
        self.assertFalse(UserItem.objects.filter(user=self.user).exists())

    def test_the_full_price_is_needed_even_if_half_comes_back(self):
        # Исход решается после проверки денег, а не до неё.
        self.gift_is_used()
        grant(self.user, self.common)
        grant(self.user, self.other)
        credit(self.user, 200, MANUAL)

        with self.assertRaises(NotEnoughFunds):
            open_case(self.user, self.case, luck=Rigged(R.COMMON))

        self.assertEqual(wallet_of(self.user).balance, 200)

    def test_a_collected_rarity_costs_half_and_gives_nothing(self):
        """Решение пользователя 18 окт 2026: ступень по честным шансам, а если она у тебя
        собрана — половина цены назад. Одной строкой «−150»: строка с плюсом ушла бы во вклад."""
        self.gift_is_used()
        grant(self.user, self.common)
        grant(self.user, self.other)
        credit(self.user, 1000, MANUAL)

        opening = open_case(self.user, self.case, luck=Rigged(R.COMMON))

        self.assertEqual((opening.item, opening.refund), (None, 150))
        self.assertEqual(wallet_of(self.user).balance, 850)
        self.assertEqual(list(BalanceLog.objects.filter(reason=BalanceLog.Reason.CASE).values_list("amount", flat=True)), [-150])

    def test_with_everything_owned_the_case_is_refused_for_free(self):
        self.gift_is_used()
        for item in (self.common, self.other, self.rare):
            grant(self.user, item)
        credit(self.user, 1000, MANUAL)

        with self.assertRaises(AllCollected):
            open_case(self.user, self.case)

        self.assertEqual(wallet_of(self.user).balance, 1000)

    def test_a_closed_case_does_not_open(self):
        self.case.active = False
        self.case.save()

        with self.assertRaises(CaseClosed):
            open_case(self.user, self.case)

        self.assertFalse(CaseOpening.objects.exists())


class CasePageTests(TestCase):
    """Кейсы на витрине, окно «что внутри» и рулетка."""

    def setUp(self):
        self.user = make_user()
        self.common = make_frame("Обычная", R.COMMON)
        self.rare = make_frame("Редкая", R.RARE)
        self.myth = make_frame("Мифическая", R.MYTHICAL)
        self.case = make_case("Рамки", 300, [self.common, self.rare, self.myth])
        self.client.force_login(self.user)  # стартовые 500

    def open(self, **extra):
        return self.client.post(reverse("case_open", args=[self.case.pk]), **extra)

    def test_the_shop_offers_the_first_case_for_free(self):
        response = self.client.get(reverse("shop"))

        case = response.context["cases"][0]
        self.assertEqual((case.cost, case.size), (0, 3))
        self.assertContains(response, "Открыть бесплатно")
        self.assertContains(response, "первый — любой — в подарок")

    def test_after_the_first_one_the_price_is_shown(self):
        open_case(self.user, self.case)

        response = self.client.get(reverse("shop"))

        self.assertContains(response, "Открыть за 300")
        self.assertNotContains(response, "Открыть бесплатно")

    def test_a_case_that_is_off_or_empty_is_not_shown(self):
        make_case("Пустой", 100, [])
        make_case("Выключенный", 100, [self.rare], active=False)

        names = [case.name for case in self.client.get(reverse("shop")).context["cases"]]

        self.assertEqual(names, ["Рамки"])

    def test_everything_owned_says_so(self):
        for item in (self.common, self.rare, self.myth):
            grant(self.user, item)

        self.assertContains(self.client.get(reverse("shop")), "Всё собрано")

    def test_whats_inside_goes_from_the_rarest_and_marks_what_is_owned(self):
        grant(self.user, self.common)

        response = self.client.get(reverse("case_card", args=[self.case.pk]))

        groups = response.context["groups"]
        # Эпической и легендарной в наборе нет — их доли разошлись по остальным: 55 + 25 + 2 = 82.
        self.assertEqual([(rarity, share) for rarity, _, share, _ in groups], [
            (R.MYTHICAL, "2,4"), (R.RARE, "30,5"), (R.COMMON, "67,1"),
        ])
        self.assertEqual([item.name for *_, items in groups for item in items if item.owned], ["Обычная"])

    def test_opening_draws_a_reel_that_stops_on_the_prize(self):
        response = self.open(headers={"HX-Request": "true"})

        reel = response.context["reel"]
        self.assertEqual(len(reel), REEL)
        self.assertEqual(reel[STOP], response.context["opening"].item)
        self.assertLessEqual(len({item.pk for item in reel}), 10)  # лента грузится разом
        self.assertContains(response, "data-prize", count=1)
        # «Уже в инвентаре» читалось как «такая уже была».
        self.assertContains(response, "Вещь добавлена в инвентарь")

    def test_on_the_shop_page_opening_asks_first(self):
        """С витрины кейс открывают мимоходом, а трата необратима: сперва «точно?»."""
        cases = self.client.get(reverse("shop")).content.decode().split('id="cases"')[1]
        post = f'hx-post="{reverse("case_open", args=[self.case.pk])}"'

        self.assertEqual(cases.count(post), 1)
        self.assertLess(cases.index('x-show="sure"'), cases.index(post))

    def test_once_more_opens_without_asking(self):
        """Под рулеткой человек уже смотрит на кейс — переспрашивать незачем."""
        roulette = self.open(headers={"HX-Request": "true"}).content.decode().split('id="cases"')[0]

        self.assertIn(f'hx-post="{reverse("case_open", args=[self.case.pk])}"', roulette)
        self.assertNotIn('x-show="sure"', roulette)

    def test_the_whole_card_opens_whats_inside(self):
        cases = self.client.get(reverse("shop")).content.decode().split('id="cases"')[1]
        link = cases.split('class="case-card-link"')[1].split(">")[0]

        self.assertIn(f'hx-get="{reverse("case_card", args=[self.case.pk])}"', link)
        self.assertIn("Что внутри кейса «Рамки»", link)

    def test_the_reel_repeats_a_few_pictures_instead_of_loading_all(self):
        """Лента грузится разом, а рамка весит до трёх мегабайт: разных картинок в ней
        не больше десяти, сколько бы вещей ни было в кейсе."""
        self.case.items.add(*[make_frame(f"Рамка {n}", R.RARE) for n in range(12)])

        reel = self.open(headers={"HX-Request": "true"}).context["reel"]

        self.assertLessEqual(len({item.pk for item in reel}), 10)

    def test_the_reel_shows_backgrounds_as_pictures(self):
        """Десяток видео фонов в едущей ленте — это десяток декодеров на четыре секунды."""
        live = make_frame("Живой фон", R.RARE, CosmeticItem.Kind.PROFILE_BACKGROUND)
        live.video.save("fon.mp4", make_mp4(), save=True)
        case = make_case("Фоны", 400, [live])

        response = self.client.post(reverse("case_open", args=[case.pk]), headers={"HX-Request": "true"})

        reel = response.content.decode().split('id="cases"')[0]
        self.assertIn(f'src="{media_url(live.image)}"', reel)
        self.assertNotIn("<video", reel)

    def test_after_the_gift_the_cases_below_lose_the_free_label_at_once(self):
        """Иначе кнопка под полками обещала бы подарок, а сервер взял бы деньги."""
        response = self.open(headers={"HX-Request": "true"})

        oob = response.content.decode().split('id="cases" hx-swap-oob="true"')[1]
        self.assertIn("Открыть за 300", oob)
        self.assertNotIn("бесплатно", oob)

    def test_a_collected_rarity_shows_something_owned_on_the_reel(self):
        open_case(self.user, self.case, luck=Rigged(R.MYTHICAL))  # подарок
        grant(self.user, self.common)

        with mock.patch("cosmetics.views.open_case", partial(open_case, luck=Rigged(R.COMMON))):
            response = self.open(headers={"HX-Request": "true"})

        self.assertEqual(response.context["reel"][STOP], self.common)
        self.assertContains(response, "у тебя уже все")
        self.assertEqual(wallet_of(self.user).balance, 350)

    def test_without_htmx_the_shop_comes_back_with_words(self):
        response = self.open(follow=True)

        self.assertRedirects(response, reverse("shop"))
        self.assertContains(response, "Выпало: ")

    def test_a_refusal_comes_back_in_words(self):
        for item in (self.common, self.rare, self.myth):
            grant(self.user, item)

        response = self.open(headers={"HX-Request": "true"})

        self.assertContains(response, "уже есть всё")
        self.assertNotContains(response, "data-prize")

    def test_opening_is_not_done_by_a_get(self):
        self.assertEqual(self.client.get(reverse("case_open", args=[self.case.pk])).status_code, 405)
        self.assertFalse(CaseOpening.objects.exists())


class CaseAdminTests(TestCase):
    def test_an_empty_case_cannot_be_switched_on(self):
        form = CaseForm(data={"name": "Пустой", "price": 300, "active": True, "order": 0})

        self.assertFalse(form.is_valid())
        self.assertIn("active", form.errors)

    def test_the_admin_shows_the_odds_and_the_average_prize(self):
        """Чтобы цену кейса ставить не вслепую: 0,6875 × 100 + 0,3125 × 250 ≈ 147."""
        case = make_case("Рамки", 300, [make_frame("Обычная", R.COMMON), make_frame("Редкая", R.RARE)])

        text = CaseAdmin(Case, site).odds_and_worth(case)

        self.assertIn("обычная 68,8 %", text)
        self.assertIn("≈ 147", text)
