from io import BytesIO, StringIO
from pathlib import Path
from tempfile import mkdtemp

from django.contrib.admin import site
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from PIL import Image as PilImage

from attachments.media import media_url
from economy.models import BalanceLog
from economy.services import NotEnoughFunds, credit, wallet_of
# Разбор mp4 и построитель коробок живут в intake: их же берёт с собой пекарня.
from intake.tests import make_mp4
from users.models import User

from . import specs
from .admin import CosmeticItemAdmin
from .forms import CosmeticItemForm
from .models import CosmeticItem, UserItem
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

    def test_in_the_shop_the_source_waits_for_the_tile_to_be_seen(self):
        # Safari на телефоне держит считанные декодеры разом — десяток автоплеев его роняет.
        item = self.make_live_background()

        page = self.client.get(reverse("shop")).content.decode()

        self.assertIn(f'data-src="{media_url(item.video)}"', page)
        self.assertIn('x-data="lazyVideo"', page)
        self.assertNotIn("autoplay", page)  # играть начнёт скрипт, когда плитку увидят


class PriceTests(TestCase):
    """Цена идёт от ступени, своя — исключение."""

    def test_the_rarity_sets_the_price(self):
        self.assertEqual(make_frame("Обычная", R.COMMON).cost, 250)
        self.assertEqual(make_frame("Мифическая", R.MYTHICAL).cost, 5000)

    def test_an_own_price_wins(self):
        item = make_frame("Особая", R.COMMON)
        item.price = 9000
        self.assertEqual(item.cost, 9000)

    def test_free_is_a_price_too(self):
        # 0 — это не «пусто»: подарок в витрине должен уметь стоить ноль.
        item = make_frame("Даром", R.EPIC)
        item.price = 0
        self.assertEqual(item.cost, 0)

    def test_an_unknown_rarity_costs_the_most(self):
        # Ошибиться в сторону «дорого» не страшно, в сторону «даром» — раздать полбакета.
        item = make_frame("Кривая", R.COMMON)
        CosmeticItem.objects.filter(pk=item.pk).update(rarity="неведомая")

        self.assertEqual(CosmeticItem.objects.get(pk=item.pk).cost, 5000)


class BuyTests(TestCase):
    """Покупка: деньги и вещь меняются местами целиком или никак."""

    def setUp(self):
        self.user = make_user()
        self.flame = make_frame("Пламя", R.RARE)  # 600

    def test_buying_takes_the_money_and_gives_the_thing(self):
        credit(self.user, 1000, MANUAL)

        buy(self.user, self.flame)

        self.assertEqual(wallet_of(self.user).balance, 400)
        self.assertTrue(UserItem.objects.filter(user=self.user, item=self.flame).exists())

    def test_the_purchase_says_in_the_journal_what_was_bought(self):
        credit(self.user, 1000, MANUAL)

        buy(self.user, self.flame)

        entry = BalanceLog.objects.filter(reason=BalanceLog.Reason.PURCHASE).get()
        self.assertEqual(entry.amount, -600)
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

        self.assertEqual(wallet_of(self.user).balance, 1400)

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
        self.cheap = make_frame("Дешёвая", R.COMMON)     # 250
        self.dear = make_frame("Дорогая", R.MYTHICAL)    # 5000
        self.client.force_login(self.user)               # вход начисляет стартовые 500

    def test_the_page_shows_prices_and_what_is_out_of_reach(self):
        response = self.client.get(reverse("shop"))

        by_name = {item.name: item for item in response.context["items"]}
        self.assertEqual(response.context["coins"], 500)
        self.assertEqual(by_name["Дешёвая"].short, 0)
        self.assertEqual(by_name["Дорогая"].short, 4500)

    def test_buying_through_the_page(self):
        response = self.client.post(reverse("item_buy", args=[self.cheap.pk]), follow=True)

        self.assertContains(response, "Куплено: Дешёвая")
        self.assertEqual(wallet_of(self.user).balance, 250)

    def test_what_is_already_owned_is_marked_instead_of_offered(self):
        self.client.post(reverse("item_buy", args=[self.cheap.pk]))

        response = self.client.get(reverse("shop"))

        by_name = {item.name: item for item in response.context["items"]}
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

    def test_the_grid_is_alive_but_loads_lazily(self):
        """Витрина показывает анимации сразу — ассортимент смотрят глазами, а на телефоне
        наведения нет вовсе. Держит это `loading=lazy`: грузится то, до чего долистали."""
        page = self.client.get(reverse("shop")).content.decode()

        self.assertIn(f'src="{media_url(self.cheap.image)}"', page)
        self.assertIn('loading="lazy"', page)


class OfferTests(TestCase):
    """Карточка товара: предпросмотр на себе, цена и кнопка."""

    def setUp(self):
        self.user = make_user()
        self.frame = make_frame("Пламя", R.COMMON)                                   # 250
        self.header = make_frame("Ночь", R.MYTHICAL, CosmeticItem.Kind.PROFILE_HEADER)  # 5000
        self.client.force_login(self.user)                                            # стартовые 500

    def card(self, item):
        return self.client.get(reverse("item_card", args=[item.pk]))

    def test_an_affordable_thing_offers_a_button(self):
        response = self.card(self.frame)

        self.assertContains(response, "Купить за 250")
        self.assertEqual(response.context["short"], 0)

    def test_an_expensive_thing_says_how_much_is_missing(self):
        response = self.card(self.header)

        self.assertEqual(response.context["short"], 4500)
        self.assertContains(response, "Не хватает 4500")
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
    получают уже принятыми. Дверь эта была закрыта — поля `video` в наборе не было."""

    def test_the_video_field_is_reachable(self):
        self.assertIn("video", CosmeticItemAdmin(CosmeticItem, site).get_fields(None))

    def test_a_replaced_file_does_not_stay_in_the_bucket(self):
        """Блоб снимается вместе с записью (post_delete в attachments/storage.py),
        но при ЗАМЕНЕ ссылка на прежний пропадала, а сам он оставался сиротой."""
        item = make_frame("Замена")
        was = item.image.name

        form = CosmeticItemForm(
            {"name": "Замена", "kind": CosmeticItem.Kind.AVATAR_FRAME, "rarity": R.COMMON, "sold": True},
            {"image": _png((224, 224))},
            instance=CosmeticItem.objects.get(pk=item.pk),
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.assertFalse(item.image.storage.exists(was))
        self.assertTrue(CosmeticItem.objects.get(pk=item.pk).image.storage.exists(
            CosmeticItem.objects.get(pk=item.pk).image.name))

    def test_an_untouched_file_survives_an_edit(self):
        item = make_frame("Тихая")
        was = item.image.name

        form = CosmeticItemForm(
            {"name": "Новое имя", "kind": CosmeticItem.Kind.AVATAR_FRAME, "rarity": R.COMMON, "sold": True},
            instance=CosmeticItem.objects.get(pk=item.pk),
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.assertTrue(item.image.storage.exists(was))

    def test_a_cleared_video_does_not_stay_in_the_bucket(self):
        item = make_frame("Была живой", R.RARE, CosmeticItem.Kind.PROFILE_BACKGROUND)
        item.video.save("fon.mp4", make_mp4(), save=True)
        was = item.video.name

        form = CosmeticItemForm(
            {"name": "Была живой", "kind": CosmeticItem.Kind.PROFILE_BACKGROUND,
             "rarity": R.RARE, "sold": True, "video-clear": "on"},
            instance=CosmeticItem.objects.get(pk=item.pk),
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.assertFalse(item.video.storage.exists(was))


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
