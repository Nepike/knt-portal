from io import BytesIO, StringIO
from pathlib import Path
from tempfile import mkdtemp

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from PIL import Image as PilImage

from users.models import User

from .models import CosmeticItem, UserItem
from .services import NotOwned, equip, grant, inventory, unequip, worn

R = CosmeticItem.Rarity


def make_user(email="u@t.local"):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


def make_frame(name="Пламя", rarity=CosmeticItem.Rarity.RARE, kind=CosmeticItem.Kind.AVATAR_FRAME):
    item = CosmeticItem(name=name, rarity=rarity, kind=kind)
    item.image.save(f"{name}.webp", _png(), save=False)
    item.still.save(f"{name}-still.webp", _png(), save=False)
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

    def test_apply_creates_an_item_with_both_pictures(self):
        self.run_it("--apply")

        item = CosmeticItem.objects.get()
        self.assertEqual(item.name, "Рамка 1")
        self.assertEqual(item.rarity, CosmeticItem.Rarity.COMMON)
        self.assertTrue(item.image.name and item.still.name)

    def test_the_cover_is_a_single_frame(self):
        self.run_it("--apply")

        with PilImage.open(CosmeticItem.objects.get().still) as cover:
            self.assertEqual(getattr(cover, "n_frames", 1), 1)
            self.assertEqual(cover.size, (224, 224))

    def test_running_it_again_adds_nothing(self):
        self.run_it("--apply")
        self.run_it("--apply")

        self.assertEqual(CosmeticItem.objects.count(), 1)
