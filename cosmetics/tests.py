import struct
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import mkdtemp

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from PIL import Image as PilImage

from attachments.media import media_url
from economy.models import BalanceLog
from economy.services import NotEnoughFunds, credit, wallet_of
from users.models import User

from . import mp4, specs
from .forms import CosmeticItemForm
from .models import CosmeticItem, UserItem
from .services import NotOwned, equip, grant, inventory, outfit, unequip, worn
from .shop import AlreadyOwned, NotForSale, buy, on_sale

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


def box(kind, payload=b""):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def tkhd(width, height):
    """Дорожка. Ширина и высота — 16.16 с фиксированной точкой в самом хвосте."""
    return box(b"tkhd", bytes(4 + 20 + 8 + 8 + 36) + struct.pack(">II", width << 16, height << 16))


def mvhd(seconds, scale=1000):
    return box(b"mvhd", bytes(4 + 8) + struct.pack(">II", scale, int(seconds * scale)))


def make_mp4(width=1920, height=1080, seconds=5.0, faststart=True, audio=False, name="fon.mp4"):
    """Настоящее дерево коробок mp4 — без картинки внутри, но заголовок честный.

    Так проверяется ровно то, что читает сайт: ffmpeg для этого не нужен ни здесь,
    ни на сервере (см. docs/media-pipeline.md).
    """
    track = box(b"trak", tkhd(width, height) + (box(b"mdia", box(b"minf", box(b"smhd", bytes(8)))) if audio else b""))
    moov = box(b"moov", mvhd(seconds) + track)
    mdat = box(b"mdat", b"\0" * 64)
    body = box(b"ftyp", b"isom" + bytes(8)) + (moov + mdat if faststart else mdat + moov)
    return SimpleUploadedFile(name, body, content_type="video/mp4")


SAMPLE = Path(__file__).parent / "testdata" / "background.mp4"


class RealMp4Tests(TestCase):
    """Настоящий файл из ffmpeg — чтобы разбор коробок проверялся не только на своих же
    выдумках. Синтетика ниже перебирает случаи по одному, а этот пришёл из жизни."""

    def test_the_header_is_read_the_same_as_a_player_would(self):
        with SAMPLE.open("rb") as handle:
            info = mp4.probe(handle)

        self.assertEqual((info["width"], info["height"]), (1920, 1080))
        self.assertAlmostEqual(info["seconds"], 2.0, places=1)
        self.assertFalse(info["audio"])

    def test_this_very_file_is_refused_for_the_reason_the_check_exists(self):
        """У образца moov лежит В КОНЦЕ: браузер не начнёт играть, пока не скачает
        весь файл. Ровно эту ошибку загрузивший у себя не увидит."""
        self.assertFalse(mp4.probe(SAMPLE.open("rb"))["faststart"])


class Mp4Tests(TestCase):
    """Разбор заголовка mp4: это чтение коробок, а не обработка видео."""

    def test_it_reads_size_length_and_order(self):
        info = mp4.probe(make_mp4(1920, 1080, seconds=4.5).file)

        self.assertEqual((info["width"], info["height"]), (1920, 1080))
        self.assertAlmostEqual(info["seconds"], 4.5, places=2)
        self.assertTrue(info["faststart"])
        self.assertFalse(info["audio"])

    def test_moov_after_mdat_is_seen_as_no_faststart(self):
        self.assertFalse(mp4.probe(make_mp4(faststart=False).file)["faststart"])

    def test_a_sound_track_is_noticed(self):
        self.assertTrue(mp4.probe(make_mp4(audio=True).file)["audio"])

    def test_something_that_is_not_mp4_is_refused(self):
        with self.assertRaises(mp4.Broken):
            mp4.probe(_png().file)


class VideoSpecTests(TestCase):
    """Приёмка видео: отказ обязан говорить, что именно перепечь."""

    def refusal(self, kind=CosmeticItem.Kind.PROFILE_BACKGROUND, **kwargs):
        return specs.check_video(kind, make_mp4(**kwargs)) or ""

    def test_a_proper_background_passes(self):
        self.assertEqual(self.refusal(), "")

    def test_no_faststart_is_refused_because_students_would_wait(self):
        self.assertIn("faststart", self.refusal(faststart=False))

    def test_sound_is_refused(self):
        self.assertIn("вуковая дорожка", self.refusal(audio=True))

    def test_the_wrong_shape_is_refused_the_same_way_as_a_picture(self):
        self.assertIn("16:9", self.refusal(width=1080, height=1080))

    def test_too_long_is_refused(self):
        self.assertIn("длиннее", self.refusal(seconds=30))

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
