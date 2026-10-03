"""Прямая загрузка в хранилище: подпись ссылки и приём уже загруженного файла.

Владельцем берём книгу — она проще всех, но проверяется код attachments.
"""
import ast
import json
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import unquote, urlsplit

from botocore.exceptions import ClientError
from django.conf import settings
from django.contrib.auth.models import Permission
from django.core import signing
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import Subject
from intake.models import MediaJob
from lectorium.models import Lecture, Playlist
from library.models import Book
from users.models import User

from . import hls
from .media import MEDIA_CACHE, file_url, hls_key, hls_url, media_key, media_url, redirect_url
from .models import File
from .r2 import R2Storage
from .storage import CONTENT_TYPES, content_type, drop_prefix, file_storage, media_storage, random_key
from .tasks import sweep_storage
from .uploads import (
    MAX_DIRECT_SIZE, MAX_FILE_SIZE, MAX_LECTURE_SIZE, MULTIPART_SALT, UPLOAD_SALT,
    max_upload_size, new_key, part_size,
)


def make_user(email="u@t.local"):
    return User.objects.create_user(
        email=email, name="Иван", surname="Иванов", password="pass12345", must_change_password=False,
    )


def fake_storage():
    """Storage с боевым API подписи: настоящий S3Storage в тестах не поднять."""
    storage = mock.MagicMock()
    storage.location = ""
    storage.bucket_name = "knt-files"
    storage.connection.meta.client.generate_presigned_url.return_value = "https://r2.example/put?sig=1"
    return storage


def r2_storage(**options):
    """Настоящее хранилище с выдуманными ключами: подпись и параметры записи считаются
    на месте, до сети дело не доходит."""
    return R2Storage(
        bucket_name="knt-files", endpoint_url="https://account.r2.example", access_key="key",
        secret_key="secret", region_name="auto", signature_version="s3v4", addressing_style="path",
        **options,
    )


@override_settings(R2_BUCKET="knt-files")
class PresignTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user()

    def setUp(self):
        self.client.force_login(self.user)

    def ask(self, name="Зорич том 1.pdf", size=1024, storage=None):
        with mock.patch("attachments.uploads.file_storage", lambda: storage or fake_storage()):
            return self.client.post(
                reverse("upload_url"), json.dumps({"name": name, "size": size}),
                content_type="application/json",
            )

    def test_returns_url_and_token(self):
        response = self.ask()
        data = response.json()
        self.assertEqual(data["url"], "https://r2.example/put?sig=1")

        payload = signing.loads(data["token"], salt=UPLOAD_SALT)
        self.assertEqual(payload["name"], "Зорич том 1.pdf")
        # Ключ выбирает сервер: свой прислать нельзя, а имя остаётся читаемым.
        self.assertRegex(payload["key"], r"^uploads/[0-9a-f]{32}/Зорич том 1\.pdf$")

    def test_the_type_is_ours_and_the_browser_is_told_which(self):
        """Тип объекта выбирает сервер и отдаёт браузеру: тот обязан прислать ровно его."""
        for name, kind in (("слайды.pptx", "application/octet-stream"), ("Зорич.pdf", "application/pdf")):
            with self.subTest(name=name):
                storage = fake_storage()
                answer = self.ask(name=name, storage=storage).json()

                signed = storage.connection.meta.client.generate_presigned_url.call_args.kwargs["Params"]
                self.assertEqual(signed["ContentType"], kind)
                self.assertEqual(answer["type"], kind)

    def test_the_signature_covers_the_type(self):
        """Настоящая подпись, без сети: заголовок Content-Type должен в неё входить —
        только тогда хранилище откажет браузеру, приславшему другой тип."""
        answer = self.ask(name="страница.xml", storage=r2_storage()).json()

        self.assertIn("X-Amz-SignedHeaders=content-type%3Bhost", answer["url"])

    def test_the_upload_script_sends_the_type_it_was_given(self):
        """Вторая половина договора живёт в скрипте. Без этой строки браузер пошлёт свой
        file.type, и хранилище откажет каждой загрузке, где он не совпал с подписанным."""
        script = (settings.BASE_DIR / "core/static/core/js/components.js").read_text(encoding="utf-8")

        self.assertIn('if (type) request.setRequestHeader("Content-Type", type);', script)

    def test_dangerous_and_huge_are_refused(self):
        self.assertEqual(self.ask(name="страница.html").status_code, 400)
        with mock.patch("attachments.views.MAX_DIRECT_SIZE", 10):
            self.assertEqual(self.ask(size=100).status_code, 400)

    def test_garbage_request(self):
        self.assertEqual(
            self.client.post(reverse("upload_url"), "не json", content_type="application/json").status_code, 400
        )
        self.assertEqual(self.client.get(reverse("upload_url")).status_code, 405)

    @override_settings(R2_BUCKET="")
    def test_unavailable_without_r2(self):
        self.assertEqual(self.ask().status_code, 409)


class MediaUrlTests(SimpleTestCase):
    """Какой адрес попадает в разметку."""

    def field(self, storage=None):
        return SimpleNamespace(
            name="images/board.png", url="/media/images/board.png",
            storage=storage or FileSystemStorage(),
        )

    def test_local_disk_in_development_goes_straight_to_the_file(self):
        self.assertEqual(media_url(self.field()), "/media/images/board.png")

    def test_signing_storage_goes_through_our_redirect(self):
        self.assertTrue(media_url(self.field(storage=object())).startswith("/img/"))

    @override_settings(FILES_BASE_URL="https://files.example")
    def test_own_domain_takes_everything_including_local_disk(self):
        # Адрес один и тот же в обоих режимах — переключение хранилища ссылок не меняет.
        self.assertTrue(media_url(self.field()).startswith("https://files.example/img/"))

    def test_empty_field_gives_empty_address(self):
        self.assertEqual(media_url(None), "")


class FileUrlTests(TestCase):
    """Адрес файла: подпись вместо сессии, имя в хвосте — для браузера."""

    def make(self, name="Зорич. Том 1.pdf"):
        book = Book.objects.create(title="Книга", status=Book.Status.APPROVED, uploader=make_user())
        return File.objects.create(book=book, name=name, file=ContentFile(b"pdf", name="z.pdf"))

    def test_address_carries_the_name_and_survives_a_slash_in_it(self):
        url = file_url(self.make(name="Лекции 1/2.pdf"))
        self.assertIn("%D0%9B", url)  # имя доехало, косая заменена
        self.assertNotIn("1/2", url)

    def test_address_is_the_same_every_time(self):
        file = self.make()
        # Иначе кеш браузера был бы бесполезен: адрес ехал бы на каждом рендере.
        self.assertEqual(file_url(file), file_url(file))

    @override_settings(FILES_BASE_URL="https://files.example")
    def test_own_domain_is_prepended(self):
        self.assertTrue(file_url(self.make()).startswith("https://files.example/f/"))


@override_settings(MEDIA_ACCEL=True)
class AccelTests(TestCase):
    """На сервере байты отдаёт nginx, приложение возвращает только заголовок."""

    def setUp(self):
        self.key = file_storage().save("images/board.png", ContentFile(b"png"))

    def test_local_storage_points_nginx_at_the_media_folder(self):
        response = self.client.get(redirect_url(self.key))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Accel-Redirect"], "/__local/" + self.key)
        self.assertEqual(response.content, b"")  # байты через приложение не идут

    def test_content_type_comes_from_the_file_name(self):
        # При X-Accel-Redirect побеждает заголовок приложения, а не mime-тип nginx:
        # с дефолтным text/html браузер показал бы pdf текстом, а картинку — ничем.
        self.assertEqual(self.client.get(redirect_url(self.key))["Content-Type"], "image/png")

        key = file_storage().save("books/Зорич.pdf", ContentFile(b"%PDF"))
        self.assertEqual(self.client.get(redirect_url(key))["Content-Type"], "application/pdf")

    def test_unknown_extension_is_handed_over_as_bytes(self):
        key = file_storage().save("books/конспект.djvu", ContentFile(b"x"))
        self.assertEqual(
            self.client.get(redirect_url(key))["Content-Type"], "application/octet-stream",
        )

    def test_redirect_is_kept_when_nginx_is_not_there(self):
        with self.settings(MEDIA_ACCEL=False):
            response = self.client.get(redirect_url(self.key))
        self.assertEqual(response.status_code, 302)

    def test_cyrillic_name_is_escaped_for_nginx(self):
        key = file_storage().save("books/Зорич том 1.pdf", ContentFile(b"pdf"))
        response = self.client.get(redirect_url(key))

        target = response["X-Accel-Redirect"]
        self.assertTrue(target.startswith("/__local/books/"))
        self.assertNotIn(" ", target)  # заголовок обязан быть ASCII
        self.assertEqual(unquote(target[len("/__local/"):]), key)


class RandomKeyTests(SimpleTestCase):
    def test_key_is_unguessable_but_keeps_the_file_name(self):
        # Бакет публичный: по предсказуемому пути библиотеку перебрали бы снаружи.
        first = random_key("books", "Зорич том 1.pdf")
        second = random_key("books", "Зорич том 1.pdf")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("books/"))
        self.assertTrue(first.endswith("/Зорич том 1.pdf"))


class MediaImageTests(TestCase):
    """Отдача картинки без nginx — так работает разработка."""

    def setUp(self):
        self.key = file_storage().save("images/board.png", ContentFile(b"png"))

    def test_redirect_leads_to_the_storage_and_may_be_cached(self):
        response = self.client.get(redirect_url(self.key))

        self.assertEqual(response.status_code, 302)
        self.assertIn(self.key, response["Location"])
        # Кеш короче подписи R2: иначе браузер переиспользовал бы протухшую ссылку.
        self.assertIn("max-age", response["Cache-Control"])

    def test_picture_opens_without_a_session(self):
        # Домен файлов другой, куки туда не приходят: разрешение даёт подпись в адресе.
        self.assertEqual(self.client.get(redirect_url(self.key)).status_code, 302)

    def test_tampered_token_is_not_found(self):
        self.assertEqual(self.client.get(redirect_url(self.key) + "x/").status_code, 404)


@override_settings(FILES_BASE_URL="https://files.example", ALLOWED_HOSTS=["testserver", "files.example"])
class FilesHostTests(TestCase):
    """Загруженное людьми отдаётся только с домена файлов.

    Подпись от домена не зависит, и тот же адрес открывался на домене сайта — то есть
    чужой файл оказывался в одном origin с куками сессии.
    """

    def setUp(self):
        key = file_storage().save("images/board.png", ContentFile(b"png"))
        book = Book.objects.create(title="Книга", status=Book.Status.APPROVED, uploader=make_user())
        self.file = File.objects.create(book=book, name="z.pdf", file=ContentFile(b"pdf", name="z.pdf"))
        self.addresses = [
            redirect_url(key),
            urlsplit(file_url(self.file)).path,
            urlsplit(hls_url("lectures/abc/0/seg1.m4s")).path,
        ]

    def test_on_the_site_host_every_file_address_leads_to_the_files_host(self):
        for address in self.addresses:
            with self.subTest(address=address.split("/")[1]):
                response = self.client.get(address + "?t=1")

                self.assertRedirects(
                    response, f"https://files.example{address}?t=1", fetch_redirect_response=False,
                )

    def test_the_files_host_itself_serves_them(self):
        for address in self.addresses:
            with self.subTest(address=address.split("/")[1]):
                response = self.client.get(address, headers={"Host": "files.example"})

                self.assertNotIn("files.example", response["Location"])  # 302 — уже в хранилище

    def test_the_host_is_compared_without_regard_to_case(self):
        """Иначе домен файлов, набранный заглавными, переадресовывал бы сам на себя."""
        response = self.client.get(self.addresses[0], headers={"Host": "FILES.example"})

        self.assertIn("/media/images/board", response["Location"])

    def test_a_download_is_not_counted_on_the_way_through(self):
        """Иначе одно скачивание по старой ссылке считалось бы дважды."""
        self.client.get(self.addresses[1])

        self.file.refresh_from_db()
        self.assertEqual(self.file.downloads, 0)

    def test_the_way_is_not_remembered_forever(self):
        """Постоянную переадресацию браузер запомнил бы и вёл бы на домен файлов и после
        того, как его отключат (R2_BUCKET и FILES_BASE_URL пусты — аварийный режим)."""
        response = self.client.get(self.addresses[0])

        self.assertTrue(response["Location"].startswith("https://files.example/"))
        self.assertEqual(response.status_code, 302)

    @override_settings(FILES_BASE_URL="")
    def test_without_a_files_host_the_site_serves_them_itself(self):
        response = self.client.get(self.addresses[0])

        self.assertIn("/media/images/board", response["Location"])

    def test_a_lecture_poster_is_asked_from_the_files_host_right_away(self):
        """Обложка шла адресом без домена — то есть с сайта, и теперь это был бы лишний круг."""
        lecture = Lecture(prefix="lectures/abc")

        self.assertTrue(lecture.poster_url().startswith("https://files.example/img/"))


MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-STREAM-INF:BANDWIDTH=3148741,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
0/index.m3u8
"""

MEDIA_PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MAP:URI="init_0.mp4"
#EXTINF:6.000000,
seg00000.m4s
#EXT-X-ENDLIST
"""

OURS = "https://knt-mipt.ru"


class ManifestRewriteTests(SimpleTestCase):
    """Плеер идёт по именам, написанным в манифесте, а у нас каждый адрес подписан —
    значит манифест нельзя отдать как есть."""

    def rewritten(self, text, key="lectures/abc/0/index.m3u8"):
        return hls.rewrite(key, text).splitlines()

    def test_a_segment_name_becomes_our_signed_address(self):
        lines = self.rewritten(MEDIA_PLAYLIST)

        self.assertTrue(lines[4].startswith("/hls/"))
        self.assertEqual(hls_key(lines[4].split("/")[2]), "lectures/abc/0/seg00000.m4s")

    def test_the_address_inside_a_tag_is_rewritten_too(self):
        """`#EXT-X-MAP` — это init-кусок, без которого сегменты не декодируются вовсе,
        а лежит его имя внутри тега, не отдельной строкой."""
        line = next(x for x in self.rewritten(MEDIA_PLAYLIST) if x.startswith("#EXT-X-MAP"))

        self.assertIn("/hls/", line)
        self.assertEqual(hls_key(line.split("/")[2]), "lectures/abc/0/init_0.mp4")

    def test_plain_tags_are_left_alone(self):
        self.assertIn("#EXT-X-TARGETDURATION:6", self.rewritten(MEDIA_PLAYLIST))
        self.assertIn("#EXT-X-ENDLIST", self.rewritten(MEDIA_PLAYLIST))

    def test_a_nested_playlist_keeps_its_folder(self):
        line = self.rewritten(MASTER, key="lectures/abc/master.m3u8")[3]

        self.assertEqual(hls_key(line.split("/")[2]), "lectures/abc/0/index.m3u8")

    def test_climbing_out_of_the_folder_is_refused(self):
        """Подпись выдаётся на ЛЮБОЙ вычисленный ключ, и `../` увела бы её на чужое."""
        with self.assertRaises(ValueError):
            self.rewritten("#EXTM3U\n../../secret/seg.m4s\n")

    def test_a_foreign_address_is_refused(self):
        with self.assertRaises(ValueError):
            self.rewritten("#EXTM3U\nhttps://zloj.example/seg.m4s\n")


class HlsDeliveryTests(TestCase):
    """Раздача кусков: манифест переписываем на лету, сегмент отдаёт хранилище."""

    def setUp(self):
        storage = file_storage()
        self.master = storage.save("lectures/abc/master.m3u8", ContentFile(MASTER.encode()))
        storage.save("lectures/abc/0/index.m3u8", ContentFile(MEDIA_PLAYLIST.encode()))
        self.segment = storage.save("lectures/abc/0/seg00000.m4s", ContentFile(b"segment"))
        cache.clear()  # манифест кешируется, а тесты кладут файлы заново

    def test_a_manifest_comes_back_rewritten(self):
        response = self.client.get(hls_url(self.master))

        self.assertEqual(response["Content-Type"], hls.MANIFEST_TYPE)
        self.assertIn("/hls/", response.content.decode())
        self.assertNotIn("0/index.m3u8", response.content.decode())

    def test_a_segment_goes_to_the_storage(self):
        response = self.client.get(hls_url(self.segment))

        self.assertEqual(response.status_code, 302)
        self.assertIn(self.segment, response["Location"])

    def test_it_opens_without_a_session(self):
        # Домен файлов другой, куки туда не приходят: разрешение даёт подпись в адресе.
        self.assertEqual(self.client.get(hls_url(self.master)).status_code, 200)

    def test_a_tampered_token_is_not_found(self):
        address = hls_url(self.master).replace("/hls/", "/hls/x", 1)

        self.assertEqual(self.client.get(address).status_code, 404)

    def test_a_signed_but_missing_piece_is_not_found(self):
        self.assertEqual(self.client.get(hls_url("lectures/abc/net.m3u8")).status_code, 404)

    def test_a_segment_is_cached_by_the_browser_for_good(self):
        """Без `immutable` браузер держит сегмент, но на каждый переспрашивает
        «не изменилось?» — и запрос всё равно доходит до нас. На пересмотре лекции
        это лишний круг на каждые 6 секунд видео, поймано на боевом трафике.

        Врать тут нечем: в ключе uuid, перезапись запрещена, а подпись входит в адрес —
        сменится ключ подписи, сменится и адрес."""
        with override_settings(MEDIA_ACCEL=True):
            rule = self.client.get(hls_url(self.segment))["Cache-Control"]

        self.assertIn("immutable", rule)
        self.assertIn("max-age=31536000", rule)

    def test_without_nginx_the_segment_is_not_cached_for_a_year(self):
        """Так живёт разработка: ответ — редирект на подписанную ссылку хранилища,
        а та живёт сутки. Год кеша означал бы, что назавтра браузер идёт по протухшей
        подписи и видео молча перестаёт заводиться."""
        response = self.client.get(hls_url(self.segment))

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("immutable", response["Cache-Control"])
        self.assertIn(f"max-age={MEDIA_CACHE}", response["Cache-Control"])

    def test_a_manifest_is_cached_but_not_forever(self):
        """Манифест — тот же неизменный кусок, но это ВХОД в набор: вечный кеш означал бы,
        что правку раздачи часть людей не увидит год."""
        rule = self.client.get(hls_url(self.master))["Cache-Control"]

        self.assertIn("max-age=3600", rule)
        self.assertNotIn("immutable", rule)

    def test_a_picture_token_does_not_open_a_lecture(self):
        """Соли разные не для красоты: подпись — это разрешение, и разрешение на кусок
        лекции не должно годиться там, где отдаются любые ключи."""
        picture = redirect_url(self.segment).split("/")[2]
        lecture = hls_url(self.segment).split("/")[2]

        self.assertIsNone(hls_key(picture))
        self.assertIsNone(media_key(lecture))


@override_settings(CSRF_TRUSTED_ORIGINS=[OURS])
class PlayerHeadersTests(TestCase):
    """Картинку браузер берёт тегом, а плеер — запросом из скрипта: без разрешения
    ответ приходит, но отдать его плееру браузер отказывается."""

    def setUp(self):
        self.key = file_storage().save("lectures/abc/master.m3u8", ContentFile(MASTER.encode()))
        cache.clear()

    def get(self, origin=OURS, method="get"):
        return getattr(self.client, method)(hls_url(self.key), headers={"Origin": origin})

    def test_our_own_page_is_allowed(self):
        self.assertEqual(self.get()["Access-Control-Allow-Origin"], OURS)

    def test_a_stranger_is_not(self):
        self.assertNotIn("Access-Control-Allow-Origin", self.get(origin="https://zloj.example"))

    def test_the_answer_varies_by_origin(self):
        """Иначе кеш отдал бы разрешение для нашей страницы кому угодно."""
        self.assertIn("Origin", self.get()["Vary"])

    def test_the_preflight_is_answered(self):
        """Перемотка внутри сегмента шлётся с Range, а его браузер спрашивает заранее."""
        answer = self.get(method="options")

        self.assertEqual(answer.status_code, 204)
        self.assertIn("Range", answer["Access-Control-Allow-Headers"])

    def segment(self):
        return hls_url(file_storage().save("lectures/abc/0/seg_1.m4s", ContentFile(b"x")))

    def test_a_segment_under_nginx_carries_no_permission_of_ours(self):
        """Байты сегмента идут из хранилища мимо нас, и разрешение ставит nginx.

        Наш заголовок туда всё равно не доезжает, зато остаётся ВТОРЫМ источником
        одного и того же. Однажды оба сработали разом, браузер увидел два разрешения,
        счёл это ошибкой и отверг ответ — так лекторий и лёг 30.08.2026.
        """
        with override_settings(MEDIA_ACCEL=True):
            response = self.client.get(self.segment(), headers={"Origin": OURS})

        self.assertIn("X-Accel-Redirect", response)
        self.assertNotIn("Access-Control-Allow-Origin", response)

    def test_a_segment_without_nginx_carries_ours(self):
        """В разработке nginx нет, ответ отдаём мы сами — и разрешение наше."""
        response = self.client.get(self.segment(), headers={"Origin": OURS})

        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(response["Access-Control-Allow-Origin"], OURS)

    def test_a_manifest_carries_ours_either_way(self):
        """Манифест мы собираем сами на каждый запрос — он через nginx не проходит,
        и разрешение на нём остаётся нашим даже на боевом."""
        with override_settings(MEDIA_ACCEL=True):
            self.assertEqual(self.get()["Access-Control-Allow-Origin"], OURS)

    def test_a_segment_is_declared_a_video_and_not_a_form(self):
        """`.m4s` не знает ни один mimetypes, и без своего списка сегмент уезжал
        браузеру как `application/octet-stream`, а из хранилища — и вовсе как
        `application/x-www-form-urlencoded`."""
        with override_settings(MEDIA_ACCEL=True):
            response = self.client.get(self.segment())

        self.assertEqual(response["Content-Type"], "video/iso.segment")


class ContentTypeTests(SimpleTestCase):
    """Чем объект объявляется браузеру. Список свой, потому что тот же самый нужен
    пекарне при заливке, а mimetypes на Windows читает реестр и отвечает по-своему."""

    def test_the_pipelines_own_extensions_are_known(self):
        self.assertEqual(content_type("0/seg1.m4s"), "video/iso.segment")
        self.assertEqual(content_type("master.m3u8"), "application/vnd.apple.mpegurl")
        self.assertEqual(content_type("0/init_0.mp4"), "video/mp4")

    def test_what_people_upload_is_shown_from_our_own_list(self):
        shown = {
            "Конспект.pdf": "application/pdf", "Фото.JPG": "image/jpeg", "семинар.mp3": "audio/mpeg",
            "запись.m4a": "audio/mp4", "main.c": "text/plain", "решение.cpp": "text/plain",
        }
        for name, kind in shown.items():
            self.assertEqual(content_type(name), kind, name)

    def test_what_a_browser_would_run_goes_out_as_bytes(self):
        """Системный список знает `.xml` как text/xml, а `.svgz` как image/svg+xml — и то
        и другое браузер исполняет. Запрещённые расширения тут же: запрет стоит на входе,
        а тип отвечает за то, что его всё-таки миновало."""
        for name in ("страница.xml", "схема.svg", "схема.svgz", "страница.html", "страница.xhtml",
                     "код.js", "слайды.pptx", "архив.tar.gz"):
            self.assertEqual(content_type(name), "application/octet-stream", name)

    def test_the_list_itself_holds_nothing_a_browser_would_run(self):
        """Сторож на будущее: список будут дополнять, а строка с svg в нём выглядит
        безобидной картинкой."""
        for suffix, kind in CONTENT_TYPES.items():
            self.assertNotRegex(kind, r"html|xml|svg|script", suffix)

    def test_the_unknown_is_a_stream_of_bytes(self):
        """Честнее, чем угадать неверно: браузер предложит сохранить, а не покажет мусор."""
        self.assertEqual(content_type("archive.чтотото"), "application/octet-stream")


class R2StorageTests(SimpleTestCase):
    """Что уезжает в бакет вместе с байтами при загрузке ЧЕРЕЗ приложение."""

    def sent(self, name, upload):
        storage = r2_storage(file_overwrite=True, object_parameters={"CacheControl": "public"})
        storage._bucket = mock.MagicMock()  # дальше него — уже сеть
        storage.save(name, upload)
        return storage._bucket.Object.return_value.upload_fileobj.call_args.kwargs["ExtraArgs"]

    def test_the_type_comes_from_the_name_and_not_from_the_browser(self):
        """Заголовок части формы пишет браузер загрузившего, то есть он сам. Попади
        этот тип в объект — с ним файл и уехал бы всем остальным."""
        upload = SimpleUploadedFile("страница.xml", b"<x/>", content_type="text/html")

        params = self.sent("books/abc/страница.xml", upload)

        self.assertEqual(params["ContentType"], "application/octet-stream")
        self.assertEqual(params["CacheControl"], "public")  # остальные параметры на месте

    def test_no_encoding_is_guessed_from_the_name(self):
        """`.svgz` для системного списка — это svg в gzip: браузер распаковал бы его сам."""
        params = self.sent("books/abc/схема.svgz", ContentFile(b"x"))

        self.assertNotIn("ContentEncoding", params)

    @override_settings(R2_BUCKET="knt-files")
    def test_this_is_the_storage_the_site_runs_on(self):
        self.assertIsInstance(media_storage(), R2Storage)


# Встроенные переменные nginx, которые раздача берёт как есть. Всё остальное обязано
# быть объявлено в самих конфигах — иначе nginx не поднимется вовсе.
NGINX_BUILTIN = {"uri", "host", "slice_range", "upstream_cache_status", "http_origin", "scheme"}


class NginxConfigTests(SimpleTestCase):
    """Два файла nginx связаны переменными, и разъезжаются они молча.

    Проверять здесь, а не на сервере, потому что там это уже авария: сниппет, попросивший
    переменную, которой нет, роняет ВЕСЬ nginx — вместе с двумя чужими сайтами на той же
    машине. Ровно так и вышло 30.08.2026.
    """

    def setUp(self):
        root = settings.BASE_DIR
        self.site = (root / "nginx.conf").read_text(encoding="utf-8")
        self.files = (root / "nginx-files.conf").read_text(encoding="utf-8")

    def declared(self, text):
        """Переменные, которые этот файл заводит сам: map ... $имя { и set $имя."""
        return set(re.findall(r"^\s*map\s+\S+\s+\$(\w+)\s*\{", text, re.M)) | set(
            re.findall(r"^\s*set\s+\$(\w+)\s", text, re.M))

    def used(self, text):
        """Переменные, которые файл читает. Строки-комментарии не в счёт — там и примеры,
        и рассказ о том, чего в конфиге уже нет."""
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        return {name for name in re.findall(r"\$(\w+)", code) if not name.isdigit()}

    def test_the_snippet_asks_for_nothing_that_is_not_declared(self):
        known = self.declared(self.site) | self.declared(self.files) | NGINX_BUILTIN
        self.assertEqual(self.used(self.files) - known, set())

    def test_the_site_file_asks_for_nothing_that_is_not_declared(self):
        known = self.declared(self.site) | NGINX_BUILTIN | {
            "remote_addr", "proxy_add_x_forwarded_for", "request_uri", "http_upgrade",
        }
        self.assertEqual(self.used(self.site) - known, set())

    def code(self, text):
        return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))

    def blocks(self, text, opening):
        """Блоки конфига по слову в начале: {то, что между словом и скобкой: тело}."""
        found, code = {}, self.code(text)
        for match in re.finditer(opening + r"\s*([^{;]*?)\s*\{", code, re.M):
            depth, at = 1, match.end()
            while depth:
                depth += {"{": 1, "}": -1}.get(code[at], 0)
                at += 1
            found.setdefault(match.group(1), code[match.end():at - 1])
        return found

    def files_host(self):
        return next(
            body for body in re.split(r"^server\s*\{", self.code(self.site), flags=re.M)
            if re.search(r"listen 443.*server_name\s+files\.inbicst\.ru;", body, re.S)
        )

    def test_the_files_host_lets_through_nothing_but_the_three_file_addresses(self):
        """Со сквозным `location /` на домене файлов отвечал весь сайт: вход, админка,
        сброс пароля. А это origin, с которого раздаётся загруженное людьми."""
        locations = self.blocks(self.files_host(), r"^\s*location")
        proxied = [header for header, body in locations.items() if "proxy_pass" in body]

        self.assertEqual(len(proxied), 1)
        self.assertIn("return 404;", locations["/"])

        allowed = re.compile(proxied[0].removeprefix("~").strip())
        for address in (
            reverse("file_download", args=["подпись", "Зорич.pdf"]),
            reverse("media_image", args=["подпись"]),
            reverse("hls_piece", args=["подпись", "master.m3u8"]),
        ):
            self.assertRegex(address, allowed)
        for address in (reverse("login"), reverse("admin:index"), reverse("upload_url"), "/"):
            self.assertNotRegex(address, allowed)

    def test_both_ways_out_forbid_guessing_the_type(self):
        """Файл, отданный потоком байтов, браузер иначе мог бы распознать как страницу."""
        locations = self.blocks(self.files, r"^location")

        for way in ("/__r2/", "/__local/"):
            self.assertIn("add_header X-Content-Type-Options nosniff always;", locations[way], way)

    def test_the_media_folder_is_not_handed_out_past_the_signature(self):
        self.assertNotIn("/srv/knt/media", self.code(self.site))

    def test_the_cors_map_says_the_same_as_the_settings(self):
        """Список источников в nginx — вынужденная копия CSRF_TRUSTED_ORIGINS: настроек
        Django nginx не читает. Копия, за которой никто не следит, однажды разъедется,
        и лекции перестанут заводиться ровно на том домене, что забыли.

        Читаем prod.py разбором, а не импортом: боевые настройки требуют переменных
        окружения, которых в тестах нет.
        """
        block = re.search(r"map \$http_origin \$cors_origin \{(.*?)\}", self.site, re.S)
        in_nginx = set(re.findall(r'"(https://[^"]+)"\s+"https://', block.group(1)))

        source = (settings.BASE_DIR / "knt" / "settings" / "prod.py").read_text(encoding="utf-8")
        found = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Assign)
            and any(getattr(target, "id", "") == "CSRF_TRUSTED_ORIGINS" for target in node.targets)
        )
        self.assertEqual(in_nginx, set(ast.literal_eval(found.value)))


# Бакет объявляем явно: без него ручки загрузки честно отвечают «прямая загрузка
# недоступна», и весь этот набор проверял бы только эту ветку. Настоящий R2_BUCKET
# берётся из .env, а он есть не у каждого, кто запускает тесты.
@override_settings(R2_BUCKET="knt-files")
class MultipartTests(TestCase):
    """Многочастная загрузка: сырьё лекции — гигабайты, одним PUT такое не принять,
    а главное — на сорока минутах отдачи связь оборвётся почти наверняка."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.storage = fake_storage()
        self.api = self.storage.connection.meta.client
        self.api.create_multipart_upload.return_value = {"UploadId": "u-1"}
        self.api.list_parts.return_value = {"Parts": [], "IsTruncated": False}
        self.patch = mock.patch("attachments.uploads.file_storage", return_value=self.storage)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def call(self, where, **body):
        return self.client.post(reverse(where), json.dumps(body), content_type="application/json")

    def start(self, size=1024 ** 3, **extra):
        return self.call("upload_start", name="lecture.mkv", size=size, **extra).json()

    def test_a_big_file_gets_a_multipart_upload(self):
        answer = self.start()

        self.assertEqual(signing.loads(answer["token"], salt=MULTIPART_SALT)["id"], "u-1")
        self.assertEqual(answer["done"], {})

    def test_the_object_gets_its_type_when_the_upload_begins(self):
        """Части типа не несут: каким загрузку начали, таким объект и соберётся."""
        self.call("upload_start", name="запись.mp4", size=1024 ** 3)
        self.call("upload_start", name="запись.mkv", size=1024 ** 3)

        kinds = [call.kwargs["ContentType"] for call in self.api.create_multipart_upload.call_args_list]
        self.assertEqual(kinds, ["video/mp4", "application/octet-stream"])

    def test_parts_are_sized_to_fit_the_ten_thousand_limit(self):
        """У S3 не больше 10 000 частей на объект. На обычном файле берём 16 МБ,
        на гигантском — крупнее, иначе частей не хватит."""
        self.assertEqual(part_size(16 * 1024 ** 3), 16 * 1024 * 1024)
        self.assertGreater(part_size(400 * 1024 ** 3), 16 * 1024 * 1024)
        self.assertLessEqual(400 * 1024 ** 3 / part_size(400 * 1024 ** 3), 10000)

    def test_a_broken_upload_picks_up_where_it_stopped(self):
        """Ради этого всё и затевалось: 16 ГБ заново человек лить не станет."""
        self.api.list_parts.return_value = {
            "Parts": [{"PartNumber": 1, "ETag": '"a"'}, {"PartNumber": 2, "ETag": '"b"'}],
            "IsTruncated": False,
        }
        token = self.start()["token"]

        again = self.start(resume=token)

        self.assertEqual(again["token"], token)
        self.assertEqual(again["done"], {"1": '"a"', "2": '"b"'})
        # Второй раз не начинали: продолжаем ту же самую загрузку.
        self.assertEqual(self.api.create_multipart_upload.call_count, 1)

    def test_what_is_already_there_is_asked_of_the_storage_not_the_browser(self):
        self.start(resume=self.start()["token"])

        self.assertTrue(self.api.list_parts.called)

    def test_a_dead_upload_starts_over_instead_of_pretending(self):
        """Загрузку могли отменить, а браузер держит токен. Отдать ему мёртвый номер —
        значит дать залить гигабайты в никуда и узнать об этом на последнем шаге."""
        token = self.start()["token"]
        self.api.list_parts.side_effect = ClientError({"Error": {"Code": "NoSuchUpload"}}, "ListParts")

        again = self.start(resume=token)

        self.assertNotEqual(again["token"], token)
        self.assertEqual(self.api.create_multipart_upload.call_count, 2)

    def test_a_forged_token_is_refused(self):
        """Ключ и номер загрузки приходят от браузера: без подписи он дописался бы в чужую."""
        forged = signing.dumps({"key": "uploads/x/y", "name": "y", "id": "u-9"}, salt="не наша соль")

        answer = self.call("upload_parts", token=forged, numbers=[1])

        self.assertEqual(answer.status_code, 400)

    def test_part_links_are_handed_out_in_portions(self):
        token = self.start()["token"]
        self.api.generate_presigned_url.return_value = "https://r2.example/part"

        urls = self.call("upload_parts", token=token, numbers=[3, 4]).json()["urls"]

        self.assertEqual(sorted(urls), ["3", "4"])
        signed = self.api.generate_presigned_url.call_args.kwargs["Params"]
        self.assertEqual(signed["UploadId"], "u-1")
        self.assertEqual(signed["PartNumber"], 4)

    def test_finishing_gives_the_same_token_a_plain_upload_would(self):
        """Дальше форма и `_adopt` не должны видеть разницы между способами загрузки."""
        token = self.start()["token"]

        answer = self.call("upload_finish", token=token, parts={"1": '"a"', "2": '"b"'}).json()

        payload = signing.loads(answer["token"], salt=UPLOAD_SALT)
        self.assertEqual(payload["name"], "lecture.mkv")
        sent = self.api.complete_multipart_upload.call_args.kwargs["MultipartUpload"]["Parts"]
        # Порядок частей обязателен: хранилище склеивает объект ровно по этому списку.
        self.assertEqual(sent, [{"PartNumber": 1, "ETag": '"a"'}, {"PartNumber": 2, "ETag": '"b"'}])

    def test_nothing_is_assembled_from_no_parts(self):
        self.assertEqual(self.call("upload_finish", token=self.start()["token"], parts={}).status_code, 400)

    def test_cancelling_reaches_the_storage(self):
        """Незаконченные части занимают место в бакете и стоят денег."""
        self.call("upload_abort", token=self.start()["token"])

        self.assertEqual(self.api.abort_multipart_upload.call_args.kwargs["UploadId"], "u-1")

    def test_a_body_without_the_asked_for_fields_is_an_answer_not_a_crash(self):
        """Тело приходит от скрипта в браузере, а скрипт бывает и чужой. Ручка обязана
        ответить отказом: пятисотка — это уже наша ошибка, а не его."""
        token = self.start()["token"]
        broken = [
            ("upload_parts", {"token": token}),                       # без numbers
            ("upload_parts", {"token": token, "numbers": "раз-два"}),  # номера не числа
            ("upload_finish", {"token": token}),                      # без parts
            ("upload_finish", {"token": token, "parts": ["a"]}),      # parts не словарь
            ("upload_start", {"name": "x.mkv", "size": "много"}),
            ("upload_url", {"name": "x.pdf", "size": "много"}),
        ]
        for where, body in broken:
            with self.subTest(where=where, body=body):
                self.assertEqual(self.call(where, **body).status_code, 400)

    def test_an_abort_without_a_token_is_simply_nothing_to_abort(self):
        self.assertEqual(self.call("upload_abort").status_code, 200)
        self.assertFalse(self.api.abort_multipart_upload.called)


class UploadLimitTests(TestCase):
    """Потолок размера. Лекции нужны десятки гигабайт, остальным столько незачем:
    место в бакете стоит денег."""

    def setUp(self):
        self.user = make_user()

    def test_an_ordinary_person_keeps_the_ordinary_ceiling(self):
        with mock.patch("attachments.uploads.direct_upload", return_value=True):
            self.assertEqual(max_upload_size(self.user), MAX_DIRECT_SIZE)

    def test_whoever_may_add_lectures_may_upload_a_lecture(self):
        self.user.user_permissions.add(Permission.objects.get(codename="add_playlist"))
        fresh = User.objects.get(pk=self.user.pk)  # права кешируются на объекте

        with mock.patch("attachments.uploads.direct_upload", return_value=True):
            self.assertEqual(max_upload_size(fresh), MAX_LECTURE_SIZE)

    def test_without_direct_upload_everything_goes_through_the_app(self):
        with mock.patch("attachments.uploads.direct_upload", return_value=False):
            self.assertEqual(max_upload_size(self.user), MAX_FILE_SIZE)


class ScheduleTests(TestCase):
    """Ночная уборка живёт расписанием, а расписание — строкой с именем задачи."""

    def test_the_schedule_points_at_a_task_that_exists(self):
        """Опечатку в имени beat находит только на боевом сервере в четыре утра,
        и то в лог: задача просто не запускается, и никто об этом не узнаёт."""
        from knt.celery import app as celery_app

        celery_app.loader.import_default_modules()
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            self.assertIn(entry["task"], celery_app.tasks, name)

    def test_the_nightly_sweep_actually_sweeps(self):
        """Через команду, а не своей копией логики: руками её запускают ровно так же."""
        with mock.patch("attachments.tasks.call_command") as command:
            sweep_storage(days=3)

        self.assertEqual(command.call_args.args, ("clean_uploads", "--apply", "--days=3"))

    def test_it_gets_more_time_than_an_ordinary_task(self):
        """Уборка обходит весь бакет — сотни запросов в чужую сеть; общий потолок
        в минуту ей мал по построению."""
        self.assertGreater(sweep_storage.soft_time_limit, settings.CELERY_TASK_SOFT_TIME_LIMIT)
        self.assertGreater(sweep_storage.time_limit, sweep_storage.soft_time_limit)


class DropPrefixBatchTests(SimpleTestCase):
    """Ветка для бакета: поштучно у двухчасовой лекции это 2400 запросов в чужую сеть.

    Локальным хранилищем её не проверить — а работать она будет как раз на бою.
    """

    def test_keys_go_in_batches_of_a_thousand(self):
        storage = fake_storage()
        storage.location = "dev"
        # Плоская папка на 2500 кусков: столько выходит у лекции часа на три.
        storage.listdir.side_effect = lambda folder: ([], [f"seg{n:05d}.m4s" for n in range(2500)])

        with mock.patch("attachments.storage.file_storage", return_value=storage):
            dropped = drop_prefix("lectures/abc/0")

        calls = storage.connection.meta.client.delete_objects.call_args_list
        sizes = [len(call.kwargs["Delete"]["Objects"]) for call in calls]

        self.assertEqual(dropped, 2500)
        self.assertEqual(sizes, [1000, 1000, 500])
        # Префикс хранилища listdir не отдаёт, а в бакете он часть ключа.
        self.assertEqual(calls[0].kwargs["Delete"]["Objects"][0]["Key"], "dev/lectures/abc/0/seg00000.m4s")


class DownloadCountTests(TestCase):
    """Счётчик скачиваний приносит токены загрузившему, а ссылка открывается без входа."""

    def setUp(self):
        cache.clear()  # отметки «уже считали» живут в кэше и переживают тесты
        book = Book.objects.create(title="Книга", status=Book.Status.APPROVED, uploader=make_user())
        self.file = File.objects.create(book=book, name="z.pdf", file=ContentFile(b"pdf", name="z.pdf"))

    def download(self, file=None, ip="10.0.0.7"):
        return self.client.get(file_url(file or self.file), REMOTE_ADDR=ip)

    def count(self, file=None):
        return File.objects.get(pk=(file or self.file).pk).downloads

    def test_the_same_address_counts_once(self):
        """Иначе цикл из ста запросов был бы сотней «скачиваний»."""
        for _ in range(3):
            self.assertEqual(self.download().status_code, 302)  # а файл отдаём каждый раз

        self.assertEqual(self.count(), 1)

    def test_different_addresses_count_separately(self):
        self.download(ip="10.0.0.7")
        self.download(ip="10.0.0.8")

        self.assertEqual(self.count(), 2)

    def test_another_file_from_the_same_address_counts_too(self):
        other = File.objects.create(book=self.file.book, name="y.pdf", file=ContentFile(b"pdf", name="y.pdf"))
        self.download()
        self.download(other)

        self.assertEqual((self.count(), self.count(other)), (1, 1))

    def test_the_mark_is_kept_for_a_day(self):
        with mock.patch("attachments.views.cache.add", return_value=True) as add:
            self.download()

        self.assertEqual(add.call_args.args[2], 24 * 3600)

    def test_a_dead_cache_serves_the_file_and_counts_nothing(self):
        """Раздача не должна зависеть от счётчика. А не считать безопаснее, чем считать
        всё подряд: счётчик — это чьи-то токены."""
        with mock.patch("attachments.views.cache.add", side_effect=ConnectionError("Redis не отвечает")):
            with self.assertLogs("attachments.views", "ERROR"):
                response = self.download()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.count(), 0)


IMMUTABLE = "public, max-age=31536000, immutable"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class RetypeTests(SimpleTestCase):
    """Починка типов у уже лежащего в бакете. Бакет — словарь {ключ: метаданные}."""

    def setUp(self):
        self.bucket = {
            "books/a/Зорич.pdf": {"ContentType": "application/pdf", "CacheControl": IMMUTABLE},
            "books/b/слайды.pptx": {"ContentType": PPTX, "CacheControl": IMMUTABLE},
            # Тип уже верный, не так только кодировка: её storages дописывал по хвосту имени.
            "books/c/данные.gz": {"ContentType": "application/octet-stream", "ContentEncoding": "gzip"},
            "uploads/d/страница.xml": {"ContentType": "text/html"},
            "lectures/e/0/seg1.m4s": {"ContentType": "application/x-www-form-urlencoded"},
        }
        self.before = {key: dict(meta) for key, meta in self.bucket.items()}

        storage = fake_storage()
        storage.listdir.return_value = (["books", "lectures", "uploads"], [])
        self.api = storage.connection.meta.client
        self.api.list_objects_v2.side_effect = lambda Bucket, Prefix: {
            "Contents": [{"Key": key} for key in self.bucket if key.startswith(Prefix)],
        }
        self.api.head_object.side_effect = lambda Bucket, Key: dict(self.bucket[Key])
        self.api.copy_object.side_effect = self.copy
        for where in ("attachments.uploads", "attachments.management.commands.storage_retype"):
            patch = mock.patch(f"{where}.file_storage", return_value=storage)
            patch.start()
            self.addCleanup(patch.stop)

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.journal = str(Path(folder.name) / "было.jsonl")

    def copy(self, Bucket, Key, CopySource, MetadataDirective, Metadata, **meta):
        """Как у настоящего хранилища: REPLACE оставляет объекту только присланное."""
        self.assertEqual((CopySource["Key"], MetadataDirective), (Key, "REPLACE"))
        self.bucket[Key] = meta

    def retype(self, *args):
        call_command("storage_retype", *args, stdout=mock.MagicMock())

    def test_a_fitting_only_looks(self):
        self.retype()

        self.assertEqual(self.bucket, self.before)

    def test_what_people_uploaded_gets_the_type_of_our_list(self):
        self.retype("--apply", "--journal", self.journal)

        self.assertEqual(self.bucket["books/b/слайды.pptx"]["ContentType"], "application/octet-stream")
        self.assertEqual(self.bucket["uploads/d/страница.xml"]["ContentType"], "application/octet-stream")
        # Верное не трогаем: копия на себя — это запрос, а их тут тысячи.
        self.assertEqual(self.api.copy_object.call_count, 3)

    def test_lecture_sets_are_left_alone_unless_asked_for(self):
        """Их миллионы кусков: один листинг этой папки длится дольше всей починки."""
        self.retype("--apply", "--journal", self.journal)

        listed = [call.kwargs["Prefix"] for call in self.api.list_objects_v2.call_args_list]
        self.assertEqual(sorted(listed), ["books/", "uploads/"])
        self.assertEqual(self.bucket["lectures/e/0/seg1.m4s"], self.before["lectures/e/0/seg1.m4s"])

    def test_an_explicit_prefix_reaches_the_lecture_sets(self):
        self.retype("--prefix", "lectures", "--apply", "--journal", self.journal)

        self.assertEqual(self.bucket["lectures/e/0/seg1.m4s"]["ContentType"], "video/iso.segment")
        self.assertEqual(self.api.copy_object.call_count, 1)

    def test_the_rest_of_the_metadata_survives_the_copy(self):
        """REPLACE стирает всё разом: без переноса объект терял бы вечный кеш."""
        self.retype("--apply", "--journal", self.journal)

        self.assertEqual(self.bucket["books/b/слайды.pptx"]["CacheControl"], IMMUTABLE)

    def test_an_encoding_guessed_from_the_name_goes_away(self):
        """С `Content-Encoding: gzip` браузер молча распаковывает архив при скачивании."""
        self.retype("--apply", "--journal", self.journal)

        self.assertEqual(self.bucket["books/c/данные.gz"], {"ContentType": "application/octet-stream"})

    def test_nothing_is_changed_without_a_journal_to_go_back_by(self):
        with self.assertRaises(CommandError):
            self.retype("--apply")

        self.assertEqual(self.bucket, self.before)

    def test_the_journal_puts_everything_back(self):
        self.retype("--apply", "--journal", self.journal)
        self.assertNotEqual(self.bucket, self.before)

        self.retype("--restore", self.journal)
        self.assertNotEqual(self.bucket, self.before)  # без --apply и возврат — только примерка

        self.retype("--restore", self.journal, "--apply")
        self.assertEqual(self.bucket, self.before)

    def test_a_journal_is_never_written_over(self):
        """Оборвавшийся запуск повторяют, и второй записал бы поверх только недочиненное —
        прежние типы уже исправленного пропали бы."""
        self.retype("--apply", "--journal", self.journal)
        kept = Path(self.journal).read_text(encoding="utf-8")
        self.bucket["uploads/d/страница.xml"]["ContentType"] = "text/html"

        with self.assertRaises(CommandError):
            self.retype("--apply", "--journal", self.journal)

        self.assertEqual(Path(self.journal).read_text(encoding="utf-8"), kept)
        self.assertEqual(self.bucket["uploads/d/страница.xml"]["ContentType"], "text/html")

    def test_a_raw_recording_too_big_to_copy_is_only_reported(self):
        self.bucket["uploads/f/запись.mkv"] = {"ContentType": "video/x-matroska", "ContentLength": 5 * 1024 ** 3}

        self.retype("--apply", "--journal", self.journal)

        self.assertEqual(self.bucket["uploads/f/запись.mkv"]["ContentType"], "video/x-matroska")
        self.assertNotIn("запись.mkv", Path(self.journal).read_text(encoding="utf-8"))


class AdoptUploadTests(TestCase):
    """Форма присылает подписанный токен вместо файла — файл уже в хранилище."""

    @classmethod
    def setUpTestData(cls):
        cls.author = make_user("a@t.local")

    def setUp(self):
        self.client.force_login(self.author)

    def put(self, key="uploads/abc/Зорич.pdf", body=b"pdf-payload"):
        return file_storage().save(key, ContentFile(body))

    def token(self, key, name="Зорич.pdf"):
        return signing.dumps({"key": key, "name": name}, salt=UPLOAD_SALT)

    def create(self, **extra):
        return self.client.post(reverse("book_new"), {"title": "Механика", **extra})

    def test_uploaded_file_becomes_a_row(self):
        key = self.put()
        self.create(uploaded=self.token(key))

        file = File.objects.get()
        self.assertEqual(file.book, Book.objects.get())
        self.assertEqual(file.name, "Зорич.pdf")
        self.assertEqual(file.file.name, key)  # ключ не перезаписан, файл заново не льётся
        self.assertEqual(file.size, len(b"pdf-payload"))
        self.assertEqual(file.uploader, self.author)

    def test_tampered_token_stops_the_save(self):
        good = self.put(key="uploads/ok/Целый.pdf")
        broken = self.put(key="uploads/bad/Подделка.pdf")
        response = self.create(uploaded=[self.token(good, name="Целый.pdf"), self.token(broken) + "x"])
        self.assertContains(response, "не доехал до хранилища")
        self.assertFalse(Book.objects.exists())

    def test_missing_object_stops_the_save(self):
        # Файл не доехал — книга НЕ сохраняется молча без него.
        response = self.create(uploaded=self.token("uploads/abc/пусто.pdf"))
        self.assertContains(response, "не доехал до хранилища")
        self.assertFalse(File.objects.exists())
        self.assertFalse(Book.objects.exists())

    def test_oversized_object_stops_the_save(self):
        key = self.put(body=b"x" * 100)
        with mock.patch("attachments.uploads.MAX_DIRECT_SIZE", 10):
            response = self.create(uploaded=self.token(key))
        self.assertContains(response, "больше")
        self.assertFalse(Book.objects.exists())
        self.assertFalse(File.objects.exists())

    def test_order_follows_the_rows_on_screen(self):
        book = Book.objects.create(title="Фихтенгольц", status=Book.Status.APPROVED, uploader=self.author)
        first, second, third = [
            File.objects.create(book=book, name=f"Том {n}.pdf", order=n - 1,
                                file=ContentFile(b"x", name=f"tom{n}.pdf"))
            for n in (1, 2, 3)
        ]

        # Перетащили третий том наверх — форма прислала pk в новом порядке.
        self.client.post(reverse("book_edit", args=[book.pk]), {
            "title": "Фихтенгольц", "order": [third.pk, first.pk, second.pk],
        })
        self.assertEqual([f.name for f in book.files.all()], ["Том 3.pdf", "Том 1.pdf", "Том 2.pdf"])

    def test_new_file_lands_after_the_reordered_ones(self):
        book = Book.objects.create(title="Книга", status=Book.Status.APPROVED, uploader=self.author)
        old = File.objects.create(book=book, name="Старый.pdf", order=0,
                                  file=ContentFile(b"x", name="old.pdf"))
        key = self.put(key="uploads/new/Новый.pdf")

        self.client.post(reverse("book_edit", args=[book.pk]), {
            "title": "Книга", "order": [old.pk], "uploaded": self.token(key, name="Новый.pdf"),
        })
        self.assertEqual([f.name for f in book.files.all()], ["Старый.pdf", "Новый.pdf"])

    def test_uploaded_file_can_be_renamed_on_the_way(self):
        key = self.put()
        self.create(uploaded=self.token(key), **{"uploaded-name": "Зорич. Том 1"})
        self.assertEqual(File.objects.get().name, "Зорич. Том 1")

    def test_blank_name_falls_back_to_the_original(self):
        key = self.put()
        self.create(uploaded=self.token(key), **{"uploaded-name": "   "})
        self.assertEqual(File.objects.get().name, "Зорич.pdf")

    def test_token_survives_a_form_error(self):
        key = self.put()
        response = self.client.post(reverse("book_new"), {"title": "", "uploaded": self.token(key)})
        # Книга не сохранилась, но файл уже в хранилище — токен возвращается в форму.
        self.assertFalse(Book.objects.exists())
        self.assertContains(response, self.token(key))
        self.assertContains(response, "загружено")

    def test_cleanup_takes_only_forgotten_uploads(self):
        attached = self.put(key="uploads/live/Книга.pdf")
        self.create(uploaded=self.token(attached, name="Книга.pdf"))
        forgotten = self.put(key="uploads/dead/Забытый.pdf")
        fresh = self.put(key="uploads/fresh/Свежий.pdf")

        storage = file_storage()
        for key in (attached, forgotten):
            os.utime(storage.path(key), (0, 0))  # состарили: свежие не трогаем принципиально

        call_command("clean_uploads", "--apply", verbosity=0)
        self.assertTrue(storage.exists(attached))  # к нему привязана запись
        self.assertTrue(storage.exists(fresh))  # загружен только что, форма ещё может дойти
        self.assertFalse(storage.exists(forgotten))

    def test_cleanup_leaves_alone_a_recording_still_waiting_for_the_bakery(self):
        """Сырьё лекции записью `File` не становится вовсе — оно живёт ключом в задании
        и ждёт, когда за ним придёт пекарня. А пекарня может стоять выключенной неделю:
        без этой оговорки уборка сносила бы сырьё из-под очереди, и лекция падала бы
        с «нет такого файла» вместо того, чтобы испечься."""
        queued = self.put(key="uploads/queued/Лекция.mkv")
        baked = self.put(key="uploads/baked/Испечённая.mkv")
        MediaJob.objects.create(recipe="lecture", source=queued)
        MediaJob.objects.create(recipe="lecture", source=baked, status=MediaJob.Status.DONE)

        storage = file_storage()
        for key in (queued, baked):
            os.utime(storage.path(key), (0, 0))
        call_command("clean_uploads", "--apply", verbosity=0)

        self.assertTrue(storage.exists(queued))
        # У закрытого задания сырьё снимает своя задача сразу после `commit`; если она
        # почему-то не доехала, остаток — обычная сирота, за неё эта команда и отвечает.
        self.assertFalse(storage.exists(baked))

    def test_cleanup_keeps_the_source_of_a_job_that_did_not_work_out(self):
        """Задание возвращают в очередь из админки — «поставил ждёт, и следующая пекарня
        возьмёт снова». Без сырья такой повтор просто упал бы второй раз."""
        key = self.put(key="uploads/failed/Лекция.mkv")
        MediaJob.objects.create(recipe="lecture", source=key, status=MediaJob.Status.FAILED)
        os.utime(file_storage().path(key), (0, 0))

        call_command("clean_uploads", "--apply", verbosity=0)

        self.assertTrue(file_storage().exists(key))

    def stale_set(self, prefix):
        """Папка готового набора в хранилище, состаренная — свежие уборка не трогает."""
        storage = file_storage()
        for name in ("master.m3u8", "poster.jpg", "0/seg00000.m4s"):
            os.utime(storage.path(storage.save(f"{prefix}/{name}", ContentFile(b"x"))), (0, 0))
        return prefix

    def test_cleanup_sweeps_a_set_nobody_points_at(self):
        """Имя папке даёт приёмка и тут же пишет его в задание. Осталась без хозяина —
        значит хозяина потеряли, а весит она гигабайты и в базе её больше нет нигде."""
        orphan = self.stale_set("lectures/бесхозный")

        call_command("clean_uploads", "--apply", verbosity=0)

        self.assertFalse(file_storage().exists(f"{orphan}/master.m3u8"))

    def test_cleanup_does_not_touch_a_set_that_is_playing(self):
        subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        playlist = Playlist.objects.create(title="Механика", subject=subject, uploader=self.author)
        live = self.stale_set("lectures/живой")
        Lecture.objects.create(playlist=playlist, title="Первая", prefix=live)
        # И папку задания, которое ещё печётся: пекарня как раз льёт в неё куски.
        baking = self.stale_set("lectures/печётся")
        MediaJob.objects.create(recipe="lecture", source="uploads/x/z.mkv", prefix=baking)

        call_command("clean_uploads", "--apply", verbosity=0)

        self.assertTrue(file_storage().exists(f"{live}/master.m3u8"))
        self.assertTrue(file_storage().exists(f"{baking}/master.m3u8"))

    def test_order_continues_after_files_sent_through_the_app(self):
        key = self.put()
        self.create(uploaded=self.token(key))
        book = Book.objects.get()

        second = self.put(key="uploads/def/Второй.pdf")
        self.client.post(reverse("book_edit", args=[book.pk]), {
            "title": "Механика", "uploaded": self.token(second, name="Второй.pdf"),
        })
        self.assertEqual([f.order for f in book.files.all()], [0, 1])


class BlobCleanupTests(TestCase):
    """Блоб уходит из хранилища вместе с записью — во всех приложениях, а не только тут."""

    def setUp(self):
        self.storage = file_storage()
        self.user = make_user()

    def test_file_and_image_blobs_are_dropped(self):
        book = Book.objects.create(title="Книга", status=Book.Status.APPROVED, uploader=self.user)
        file = File.objects.create(book=book, name="скан.pdf", file=ContentFile(b"pdf", name="скан.pdf"))
        key = file.file.name

        file.delete()
        self.assertFalse(self.storage.exists(key))

    def test_photo_of_a_user_is_dropped(self):
        # Фото живёт в чужом приложении: раньше сигнал был только на File и Image,
        # и такие блобы оставались в бакете навсегда.
        self.user.photo = ContentFile(b"png", name="avatar.png")
        self.user.save()
        key = self.user.photo.name

        self.user.delete()
        self.assertFalse(self.storage.exists(key))

    def test_cascade_drops_the_blob_too(self):
        book = Book.objects.create(title="Книга", status=Book.Status.APPROVED, uploader=self.user)
        key = File.objects.create(book=book, name="скан.pdf", file=ContentFile(b"pdf", name="скан.pdf")).file.name

        book.delete()  # файл уезжает каскадом, сигнал обязан сработать и на нём
        self.assertFalse(self.storage.exists(key))

    def test_key_fits_the_database_field(self):
        # Ключ длиннее поля не сохранился бы вовсе: DataError на вставке.
        long_name = "Лекции по математическому анализу за третий семестр, поток Иванова.pdf"
        for folder in ("materials", "books", "images", "avatars", "teachers"):
            key = random_key(folder, long_name)
            self.assertLessEqual(len(key), 100, folder)
            self.assertTrue(key.endswith(".pdf"), key)

    def test_a_direct_upload_key_fits_it_too(self):
        """Прямая загрузка ходит мимо `upload_to` и кладёт ключ в поле сама. Имя ей
        приходит от браузера, обрезанное только до 150 символов, — и без этой же мерки
        обычный студенческий заголовок давал пятисотку вместо сохранённого материала."""
        long_name = "Конспект по общей физике. Механика и термодинамика. 1 семестр 2024.pdf"
        key = new_key(long_name)

        self.assertLessEqual(len(key), 100)
        self.assertTrue(key.startswith("uploads/"), key)
        self.assertTrue(key.endswith(".pdf"), key)
        # Папка на ключ по-прежнему своя: по ней `drop_source` снимает сырьё целиком.
        self.assertEqual(len(key.split("/")), 3, key)
        # И он правда ложится в базу — ради этого всё и затевалось.
        book = Book.objects.create(title="Книга", status=Book.Status.APPROVED, uploader=self.user)
        File.objects.create(book=book, name="конспект", file=key, size=1)

    def test_name_without_extension_survives(self):
        key = random_key("books", "конспект")
        self.assertTrue(key.endswith("/конспект"), key)
