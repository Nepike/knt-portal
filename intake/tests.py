import argparse
import contextlib
import io
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import sys
import urllib.error
from datetime import timedelta
from math import ceil
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib import admin
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from attachments.models import File
from core.models import Subject
from lectorium.models import Lecture, Playlist
from materials.models import Material
from tools import bake
from users.models import User

from . import mp4, spec, views
from .admin import MediaJobAdmin
from .models import CLAIM_TIMEOUT, MAX_TRIES, MediaJob
from .spec import MASTER, POSTER
from .tasks import drop_source


def make_lecture():
    subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
    author = User.objects.create_user(
        email="a@t.local", name="Иван", surname="Иванов", password="pass12345",
        must_change_password=False,
    )
    playlist = Playlist.objects.create(title="Механика", subject=subject, uploader=author)
    return Lecture.objects.create(playlist=playlist, title="Первая", prefix="")

TOKEN = "OqRZ7yTn3xK1pWvB2mLd8sFhJc0aEg"  # как из token_urlsafe, только покороче


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


class RealMp4Tests(SimpleTestCase):
    """Настоящий файл из ffmpeg — чтобы разбор коробок проверялся не только на своих же
    выдумках. Синтетика ниже перебирает случаи по одному, а этот пришёл из жизни."""

    def test_the_header_is_read_the_same_as_a_player_would(self):
        with SAMPLE.open("rb") as handle:
            info = mp4.probe(handle)

        self.assertEqual((info["width"], info["height"]), (1920, 1080))
        self.assertAlmostEqual(info["seconds"], 2.0, places=1)
        self.assertFalse(info["audio"])

    def test_this_very_file_is_refused_for_the_reason_the_check_exists(self):
        """У образца moov лежит В КОНЦЕ. Ровно эту ошибку загрузивший у себя не увидит:
        файл у него на диске и открывается мгновенно."""
        with SAMPLE.open("rb") as handle:
            self.assertFalse(mp4.probe(handle)["faststart"])


class Mp4Tests(SimpleTestCase):
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
            mp4.probe(SimpleUploadedFile("a.png", b"\x89PNG\r\n\x1a\n" + bytes(64)).file)


class RecipeTests(SimpleTestCase):
    """Сверка файла с рецептом. Этой же функцией пекарня проверяет себя перед отправкой,
    поэтому отказ обязан называть, что именно перепечь."""

    def refusal(self, name="cosmetic-background", size=1024, **kwargs):
        upload = make_mp4(**kwargs)
        return spec.check(spec.RECIPES[name], mp4.probe(upload.file), size) or ""

    def test_a_proper_background_passes(self):
        self.assertEqual(self.refusal(), "")

    def test_no_faststart_is_refused_because_students_would_wait(self):
        self.assertIn("faststart", self.refusal(faststart=False))

    def test_sound_is_refused(self):
        self.assertIn("вуковая дорожка", self.refusal(audio=True))

    def test_the_wrong_size_is_refused_with_both_numbers(self):
        problem = self.refusal(width=1280, height=720)

        self.assertIn("1920×1080", problem)
        self.assertIn("1280×720", problem)

    def test_too_long_is_refused(self):
        self.assertIn("длиннее", self.refusal(seconds=30))

    def test_too_heavy_is_refused(self):
        self.assertIn("тяжелее", self.refusal(size=9 * spec.MB))

    def test_a_header_is_measured_by_its_own_recipe(self):
        """Одна ошибка на два рецепта: фон в слоте шапки проходил бы, будь мерка общая."""
        self.assertEqual(self.refusal("cosmetic-header", width=1536, height=256), "")
        self.assertIn("1536×256", self.refusal("cosmetic-header"))

    def test_what_the_site_sends_turns_back_into_the_same_recipe(self):
        """Круг «сайт отдал → пекарня собрала» обязан быть без потерь: по неполному
        рецепту вышел бы правдоподобный файл, который отвалится уже на приёмке."""
        back = {name: spec.recipe_from(data) for name, data in spec.payload().items()}

        self.assertEqual(back, spec.RECIPES)

    def test_a_field_the_bakery_does_not_know_is_not_ignored(self):
        with self.assertRaises(ValueError) as caught:
            spec.recipe_from({**spec.payload()["cosmetic-header"], "bitrate": 1})

        self.assertIn("bitrate", str(caught.exception))


@override_settings(INTAKE_TOKEN=TOKEN)
class SpecEndpointTests(SimpleTestCase):
    """Пекарня читает рецепт у сайта: её копия репозитория может быть недельной
    давности, а требования — сегодняшними."""

    def get(self, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.get(reverse("intake_spec"), headers=headers)

    def test_the_numbers_come_out_as_they_are_written(self):
        answer = self.get().json()

        self.assertEqual(answer["cosmetic-background"]["width"], 1920)
        self.assertEqual(answer["cosmetic-background"]["seconds"], 8)
        self.assertEqual(set(answer), set(spec.RECIPES))

    def test_a_stranger_is_not_let_in(self):
        self.assertEqual(self.get(token=None).status_code, 401)
        self.assertEqual(self.get(token="ne-tot-token").status_code, 401)

    def test_a_strange_header_is_refused_and_not_a_crash(self):
        """Заголовок — чужой ввод. Свой токен проверен настройкой, присланный проверить
        нельзя, и сравнение за постоянное время об нелатиницу спотыкается."""
        self.assertEqual(self.get(token="токен-кириллицей").status_code, 401)

    def test_the_refusal_says_what_header_is_missing(self):
        """Отвечаем скрипту, а не браузеру: голое «401» отлаживать нечем."""
        self.assertIn("Authorization", self.get(token=None).json()["error"])

    @override_settings(INTAKE_TOKEN="")
    def test_an_unconfigured_intake_lets_nobody_in(self):
        """Ручка открыта в интернет без логина, поэтому забытая настройка обязана
        закрывать дверь, а не открывать её всем."""
        self.assertEqual(self.get().status_code, 503)
        self.assertIn("INTAKE_TOKEN", self.get().json()["error"])

    @override_settings(INTAKE_TOKEN="токен-кириллицей")
    def test_a_token_no_client_could_send_counts_as_unconfigured(self):
        """Заголовки HTTP переносят только латиницу — такой токен пекарня физически
        не отправит. Принимать его значило бы завести настройку, которая проходит
        тесты и не работает ни у кого."""
        answer = self.get(token="токен-кириллицей")

        self.assertEqual(answer.status_code, 503)
        self.assertIn("латиницы", answer.json()["error"])

    @override_settings(INTAKE_TOKEN="хвост после решётки # заметка")
    def test_a_token_with_a_comment_stuck_to_it_is_called_broken(self):
        """django-environ не срезает хвост после решётки, и `INTAKE_TOKEN=abc # заметка`
        даёт значение вместе с заметкой. Лучше назвать сломанным сразу."""
        self.assertEqual(self.get().status_code, 503)


def made(duration=600.0, heights=(1080, 720), segments=None, poster=1024):
    """Описание готового набора HLS — то, что пекарня сообщает о своей работе."""
    segments = ceil(duration / spec.RECIPES["lecture"].segment) if segments is None else segments
    return {
        "duration": duration, "poster_bytes": poster,
        "renditions": [
            {"height": height, "width": height * 16 // 9, "segments": segments, "bytes": 10 ** 7}
            for height in heights
        ],
    }


class LadderTests(SimpleTestCase):
    """Набор HLS меряется описанием, а не файлами: их тысячи, и открыть каждый
    не может ни сайт, ни пекарня."""

    def refusal(self, **kwargs):
        return spec.check_ladder(spec.RECIPES["lecture"], made(**kwargs)) or ""

    def test_a_proper_set_passes(self):
        self.assertEqual(self.refusal(), "")

    def test_a_shorter_recording_gets_fewer_segments(self):
        self.assertEqual(self.refusal(duration=101.0, segments=17), "")

    def test_a_broken_off_bake_is_caught_by_the_segment_count(self):
        """Самый вероятный способ получить полулекцию: выпечка оборвалась, манифест
        короче длительности. Снаружи такой набор выглядит совершенно целым."""
        problem = self.refusal(duration=600.0, segments=40)

        self.assertIn("40 сегментов", problem)
        self.assertIn("100", problem)

    def test_one_segment_off_is_forgiven(self):
        """ffmpeg режет по опорным кадрам и на границе может сдвинуться на один."""
        exact = ceil(600.0 / spec.RECIPES["lecture"].segment)

        self.assertEqual(self.refusal(segments=exact + 1), "")
        self.assertEqual(self.refusal(segments=exact - 1), "")

    def test_a_track_above_the_top_rung_is_refused(self):
        self.assertIn("1440p", self.refusal(heights=(1440, 720)))

    def test_a_lower_track_alone_is_fine(self):
        """Из записи 720p дорожки 1080p не бывает, и это не повод отказывать."""
        self.assertEqual(self.refusal(heights=(720,)), "")

    def test_no_tracks_at_all(self):
        self.assertIn("ни одной", self.refusal(heights=()))

    def test_repeated_tracks(self):
        self.assertIn("повторяются", self.refusal(heights=(720, 720)))

    def test_a_heavy_poster_is_refused(self):
        self.assertIn("обложка", self.refusal(poster=3 * spec.MB))


class RungTests(SimpleTestCase):
    """Какие дорожки печь. Вверх не растягиваем никогда."""

    def rungs(self, height):
        return bake.rungs(spec.RECIPES["lecture"], height)

    def test_a_full_size_recording_gets_both(self):
        self.assertEqual(self.rungs(1080), [1080, 720])

    def test_a_smaller_recording_is_not_blown_up(self):
        self.assertEqual(self.rungs(720), [720])

    def test_a_bigger_recording_is_brought_down_to_the_top_rung(self):
        self.assertEqual(self.rungs(2160), [1080, 720])

    def test_a_recording_below_every_rung_is_taken_as_it_is(self):
        """Лекция в 480p лучше, чем никакой."""
        self.assertEqual(self.rungs(480), [480])

    def test_an_odd_height_is_made_even(self):
        """Округляем только там, где ни одна ступень не подошла и высота берётся
        от исходника: yuv420p нечётной не бывает, а 479 приезжает после чужого кропа."""
        self.assertEqual(self.rungs(479), [478])

    def test_just_under_a_rung_falls_to_the_next_one(self):
        """1079 — это не «почти 1080»: растягивать вверх нельзя даже на пиксель."""
        self.assertEqual(self.rungs(1079), [720])


class LectureFilterTests(SimpleTestCase):
    """Цепочка фильтров: чистим и режем на ОДНОМ декодировании, иначе самая дорогая
    часть — чистка — делалась бы заново для каждой дорожки."""

    def test_two_tracks_share_one_decode(self):
        chain = bake.lecture_filter([1080, 720], 30, 60, "1:2:3:5")

        self.assertIn("split=2[s0][s1]", chain)
        self.assertIn("[s0]scale=-2:1080[v0]", chain)
        self.assertIn("[s1]scale=-2:720[v1]", chain)

    def test_one_track_needs_no_split(self):
        self.assertEqual(bake.lecture_filter([720], 30, 30, ""), "[0:v]scale=-2:720[v0]")

    def test_frames_are_dropped_only_when_there_are_extra(self):
        self.assertIn("fps=30", bake.lecture_filter([720], 30, 60, ""))
        # Обратное было бы дорисовыванием кадров из ничего.
        self.assertNotIn("fps=", bake.lecture_filter([720], 30, 25, ""))

    def test_denoising_can_be_turned_off(self):
        self.assertIn("hqdn3d=1:2:3:5", bake.lecture_filter([720], 30, 30, "1:2:3:5"))
        self.assertNotIn("hqdn3d", bake.lecture_filter([720], 30, 30, ""))


class ManifestAttributeTests(SimpleTestCase):
    def test_a_comma_inside_quotes_does_not_split_the_line(self):
        line = '#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"'

        values = bake._attributes(line)

        self.assertEqual(values["RESOLUTION"], "1920x1080")
        self.assertEqual(values["CODECS"], "avc1.640028,mp4a.40.2")


class TokenAgreementTests(SimpleTestCase):
    """Сайт и пекарня обязаны одинаково отвечать на вопрос «таким токеном можно?».

    Однажды разошлись: сайт принимал кириллический токен, а пекарня физически не могла
    его отправить — заголовки HTTP кодируются latin-1. Получалась настройка, которая
    проходила тесты и не работала ни у кого.
    """

    CASES = ["OqRZ7yTn3xK1", "abc#note", "токен-кириллицей", "with space", "a\tb"]

    def test_both_sides_call_the_same_tokens_unusable(self):
        for token in self.CASES:
            with self.subTest(token=token):
                self.assertEqual(bool(views.unusable(token)), bool(bake.token_problem(token)))

    def test_every_door_the_bakery_knocks_on_exists(self):
        """Адреса ручек пекарня складывает строкой, а не берёт по имени — своего Django
        у неё нет. Опечатку в такой строке ловить нечем, и нашлась бы она посреди
        выпечки, когда работа уже сделана, а сказать о ней некому.
        """
        source = Path(bake.__file__).read_text(encoding="utf-8")
        doors = sorted(set(re.findall(r'"(/intake/[a-z]+/)"', source)))

        self.assertIn("/intake/claim/", doors)  # регулярка сама могла разъехаться с кодом
        for door in doors:
            with self.subTest(door):
                self.assertTrue(resolve(door))


class LiveOut(io.StringIO):
    """Вывод, который считает себя живой консолью: по этому пекарня и различает
    переписывание строки на месте и запись в лог."""

    def isatty(self):
        return True


class FakeFFmpeg:
    """Поддельный ffmpeg: отдаёт заготовленные строки хода, жалуется в отведённый ему
    stderr и помнит, убили ли его."""

    def __init__(self, lines, code=0, noise=""):
        self.lines, self.code, self.noise = lines, code, noise
        self.killed, self.args = False, None

    def __call__(self, args, **kwargs):
        self.args, self.stdout = args, iter(self.lines)
        if self.noise:
            kwargs["stderr"].write(self.noise)
        return self

    def wait(self):
        return self.code

    def kill(self):
        self.killed = True


class BakeryTests(SimpleTestCase):
    """Пекарня без ffmpeg: то, что можно проверить, не запуская кодировщик."""

    def test_a_recipe_can_be_named_short(self):
        self.assertEqual(bake.pick(spec.RECIPES, "background")[0], "cosmetic-background")
        self.assertEqual(bake.pick(spec.RECIPES, "cosmetic-header")[0], "cosmetic-header")
        self.assertEqual(bake.pick(spec.RECIPES, "lecture")[0], "lecture")

    def test_an_unknown_recipe_lists_the_known_ones(self):
        with self.assertRaises(SystemExit) as caught:
            bake.pick(spec.RECIPES, "лекция")

        self.assertIn("cosmetic-background", str(caught.exception))

    def test_nvenc_gets_the_flag_without_which_quality_is_ignored(self):
        """У NVENC -cq работает только при -rc vbr и НУЛЕВОМ -b:v: с ненулевым он
        держит битрейт, а качество молча ни на что не влияет."""
        args = bake.quality_args(bake.NVENC, 26)

        self.assertEqual(args[args.index("-b:v") + 1], "0")
        self.assertEqual(args[args.index("-cq") + 1], "26")

    def test_the_processor_encoder_takes_crf(self):
        self.assertIn("-crf", bake.quality_args("libx264", 23))

    def test_offline_means_the_local_copy_and_says_so(self):
        known, whence = bake.recipes("https://knt-mipt.ru", "OqRZ7yTn3xK1", offline=True)

        self.assertEqual(known, spec.RECIPES)
        self.assertIn("локальная", whence)

    def test_an_error_page_that_is_not_json_still_names_the_code(self):
        """Ручка, которой на сайте ещё нет, отвечает 404 HTML-страницей, а 502 приезжает
        от nginx. Разбор в лоб падал бы JSONDecodeError как раз на самой полезной
        ошибке — «пекарня новее сайта»."""
        page = io.BytesIO(b"<html>Not Found</html>")
        broken = urllib.error.HTTPError("https://knt-mipt.ru/intake/release/", 404, "Not Found", {}, page)

        with mock.patch.object(bake.urllib.request, "urlopen", side_effect=broken), \
                self.assertRaises(SystemExit) as caught:
            bake.talk("https://knt-mipt.ru", "OqRZ7yTn3xK1", "/intake/release/", {})

        self.assertIn("404", str(caught.exception))

    def test_a_dropped_connection_returns_the_job_instead_of_burning_it(self):
        """Связь отвалилась посреди задания. Лекция тут ни при чём, печь её можно, —
        а `fail` зажёг бы человеку красное «не обработалась» из-за чужого VPN.

        И главное: отчитаться пекарня в эту минуту тоже не может, связи-то нет.
        Падение на самом отчёте убивало демона ровно тогда, когда он должен был пережить
        осечку и взять следующее задание, — так и случилось 02.09.2026.
        """
        job = {"id": 17, "recipe": "lecture", "token": "подпись",
               "source": "https://r2/get", "name": "zapis.mkv"}
        knocked = []

        def talk(site, token, where, body):
            knocked.append(where)
            if where == "/intake/claim/":
                return {"job": job}
            raise urllib.error.URLError("подключение отвергнуто")

        with mock.patch.object(bake, "talk", talk), mock.patch.object(bake, "say"), \
                mock.patch.object(bake, "fetch_source", side_effect=urllib.error.URLError("оборвалось")):
            worked = bake.serve_once("https://knt-mipt.ru", "OqRZ7yTn3xK1", "ffmpeg",
                                     bake.NVENC, "", spec.RECIPES)

        self.assertFalse(worked)  # вернулись живыми — и с просьбой передохнуть, см. ниже
        self.assertIn("/intake/release/", knocked)
        self.assertNotIn("/intake/fail/", knocked)

    def test_after_a_dropped_connection_the_bakery_does_not_come_straight_back(self):
        """Возвращённое задание стоит в голове очереди, и через секунду пекарня получила бы
        его же. Так 30.09.2026 одно задание прокрутилось 32767 раз — пока у сайта
        не переполнился счётчик попыток и `claim` не начал падать пятисоткой у всех."""
        job = {"id": 17, "recipe": "lecture", "token": "подпись",
               "source": "https://r2/get", "name": "zapis.mkv"}
        claims = iter([{"job": job}, KeyboardInterrupt])

        def talk(site, token, where, body):
            if where != "/intake/claim/":
                return {}
            answer = next(claims)
            if answer is KeyboardInterrupt:
                raise answer
            return answer

        slept = []
        with mock.patch.object(bake, "talk", talk), mock.patch.object(bake, "say"), \
                mock.patch.object(bake, "fetch_source", side_effect=urllib.error.URLError("оборвалось")), \
                mock.patch.object(bake.time, "sleep", slept.append):
            bake.serve(argparse.Namespace(once=False, every=60), "ffmpeg", bake.NVENC, "",
                       spec.RECIPES, "https://knt-mipt.ru", "OqRZ7yTn3xK1")

        self.assertEqual(slept, [60])

    def test_a_missing_source_is_a_failure_and_not_a_dropped_connection(self):
        """HTTPError — тоже OSError, и 404 на сырьё уходил в `release`: задание, которое
        не испечётся никогда, возвращалось в очередь и выдавалось снова, по кругу.
        Человеку же надо сказать, что запись придётся загрузить заново."""
        job = {"id": 17, "recipe": "lecture", "token": "подпись",
               "source": "https://r2/get", "name": "zapis.mkv"}
        told = {}

        def talk(site, token, where, body):
            told[where] = body
            return {"job": job} if where == "/intake/claim/" else {}

        missing = urllib.error.HTTPError("https://r2/get", 404, "Not Found", {}, io.BytesIO(b""))
        with mock.patch.object(bake, "talk", talk), mock.patch.object(bake, "say"), \
                mock.patch.object(bake.urllib.request, "urlopen", side_effect=missing):
            bake.serve_once("https://knt-mipt.ru", "OqRZ7yTn3xK1", "ffmpeg", bake.NVENC, "", spec.RECIPES)

        self.assertNotIn("/intake/release/", told)
        self.assertIn("загрузить заново", told["/intake/fail/"]["error"])

    def test_a_site_that_does_not_answer_at_all_leaves_the_daemon_waiting(self):
        """`talk` заворачивает в SystemExit только ОТВЕТЫ сайта, а «не отвечает вовсе»
        приезжает сырым URLError — мимо ветки «приёмка молчит»."""
        args = argparse.Namespace(once=True, every=1)

        with mock.patch.object(bake, "serve_once", side_effect=urllib.error.URLError("нет связи")), \
                mock.patch.object(bake, "say"):
            code = bake.serve(args, "ffmpeg", bake.NVENC, "", spec.RECIPES, "https://knt-mipt.ru", "tok")

        self.assertEqual(code, 0)

    def test_stopping_the_bakery_hands_the_job_back_instead_of_failing_it(self):
        """Ctrl+C — обычный способ выключить пекарню на ночь. Сказать сайту `fail`
        значило бы зажечь человеку красное «не обработалась» и оставить его так
        навсегда: из отказа задание вынимают руками. Поэтому `release` — и лекция
        просто ждёт следующей машины.

        Заодно проверяем, что прерывание идёт дальше: проглоти пекарня его здесь,
        Ctrl+C переставал бы её останавливать.
        """
        job = {"id": 1, "recipe": "lecture", "token": "подпись",
               "source": "https://r2/get", "name": "zapis.mkv"}
        knocked = []

        def talk(site, token, where, body):
            knocked.append(where)
            return {"job": job} if where == "/intake/claim/" else {}

        with mock.patch.object(bake, "talk", talk), mock.patch.object(bake, "say"), \
                mock.patch.object(bake, "fetch_source", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            bake.serve_once("https://knt-mipt.ru", "OqRZ7yTn3xK1", "ffmpeg", bake.NVENC, "", spec.RECIPES)

        self.assertIn("/intake/release/", knocked)
        self.assertNotIn("/intake/fail/", knocked)

    def test_a_signal_stops_the_bakery_the_same_way_ctrl_c_does(self):
        """Ctrl+C — не единственный способ выключить пекарню: systemd посылает SIGTERM,
        `tmux kill-session` и закрытая консоль — SIGHUP. Умри пекарня от них молча,
        и выключение стоило бы часа: задание никому не вернули, и запись висит человеку
        «обрабатывается», пока сайт не сочтёт машину упавшей.

        Запускаем через `main`, а не зовём `catch_stop` напрямую: сама подмена
        обработчиков стоит одной строки, а вот забыть её на пути запуска — ровно то,
        что тут может сломаться и чего снаружи не видно.

        Проверяем обработчик, а не доставку сигнала: послать себе SIGHUP посреди прогона
        нечем — на windows его нет вовсе, а на linux он оборвал бы тесты.
        """
        before = {name: signal.getsignal(getattr(signal, name)) for name in bake.STOP_SIGNALS}
        try:
            with mock.patch.object(bake, "find_ffmpeg", return_value="ffmpeg"), \
                    mock.patch.object(bake, "nvenc_problem", return_value=None), \
                    mock.patch.object(bake, "serve", return_value=0), \
                    mock.patch.object(bake, "say"):
                bake.main(["--once", "--offline"])
            for name in bake.STOP_SIGNALS:
                number = getattr(signal, name)
                with self.subTest(name), self.assertRaises(KeyboardInterrupt):
                    signal.getsignal(number)(number, None)
        finally:
            for name, handler in before.items():
                signal.signal(getattr(signal, name), handler)

        self.assertIn("SIGTERM", bake.STOP_SIGNALS)  # список сам мог остаться пустым

    def test_the_daemon_says_which_encoder_it_will_use(self):
        """Молчаливый откат на процессор — самая дорогая неожиданность из возможных:
        двухчасовая лекция печётся часы вместо двадцати минут. Разовая выпечка кодировщик
        печатала, а демон молчал, и узнавали об этом по времени первого задания.
        """
        args = argparse.Namespace(once=True, every=1)
        lines = []

        with mock.patch.object(bake, "serve_once", return_value=False), \
                mock.patch.object(bake, "say", lines.append):
            bake.serve(args, "ffmpeg", "libx264", "", spec.RECIPES, "https://knt-mipt.ru", "tok")

        said = "\n".join(lines)
        self.assertIn("libx264", said)
        self.assertNotIn("видеокарте", said)  # на процессоре — и не притворяемся картой
        self.assertIn("видеокарте", bake.encoder_name(bake.NVENC))  # а на карте — говорим

    def start(self, argv, trouble):
        """Запустить пекарню с заданной бедой вместо видеокарты. Возвращает мок `serve`.

        `--offline` тут обязателен: иначе требования пошли бы спрашивать у живого сайта,
        адрес и токен которого лежат в `.env` разработчика.
        """
        with mock.patch.object(bake, "find_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(bake, "nvenc_problem", return_value=trouble), \
                mock.patch.object(bake, "serve", return_value=0) as served, \
                mock.patch.object(bake, "say"):
            bake.main([*argv, "--offline"])
        return served

    def test_without_a_card_the_bakery_refuses_to_take_lectures(self):
        """Вся затея — про машины с видеокартой: на процессоре двухчасовая запись печётся
        часами, и сайт за это время отдаст задание другой машине (так и вышло с #210).
        Спрашиваем на входе, а не у задания: взять его и тут же вернуть значило бы
        отбирать работу у той, что с картой.

        И причину называем ту, что сказал ffmpeg: «нет драйвера», «ffmpeg без nvenc»
        и «карты не видно» — три разные починки, выяснять их руками человек не должен.
        """
        with self.assertRaises(SystemExit) as caught:
            self.start(["--serve"], "Cannot load libcuda.so.1")

        self.assertIn("Cannot load libcuda.so.1", str(caught.exception))
        self.assertIn("--cpu", str(caught.exception))  # и как настоять, если всё же надо

    def test_a_lecture_baked_by_hand_needs_the_card_too(self):
        """Разовая выпечка лекции — та же работа и то же железо. А ролику косметики
        карта не нужна: там восемь секунд, процессор берёт их мгновенно."""
        with self.assertRaises(SystemExit) as caught:
            self.start(["lecture", str(Path(bake.__file__))], "No capable devices found")

        self.assertIn("No capable devices found", str(caught.exception))

    def test_the_cpu_switch_is_still_obeyed(self):
        """`--cpu` — решение человека, а не случайный откат, и запрет его не касается."""
        served = self.start(["--serve", "--cpu"], "Cannot load libcuda.so.1")

        self.assertEqual(served.call_args[0][2], "libx264")  # третьим идёт кодировщик

    def test_the_first_line_of_the_complaint_is_what_reaches_the_human(self):
        """ffmpeg жалуется с меткой адреса — человеку она не нужна, а причина нужна."""
        noisy = subprocess.CompletedProcess([], 1, "", "[h264_nvenc @ 0x55f] Cannot load libcuda.so.1\n"
                                                       "Error initializing output stream 0:0\n")

        with mock.patch.object(bake, "run", return_value=noisy):
            self.assertEqual(bake.nvenc_problem("ffmpeg"), "Cannot load libcuda.so.1")

        with mock.patch.object(bake, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertIsNone(bake.nvenc_problem("ffmpeg"))

    def test_the_long_phases_tell_the_site_the_machine_is_alive(self):
        """Скачивание и выпечка не обращаются к сайту вовсе, а вместе это часы. Пока срок
        шёл от захвата, сайт отбирал задание у работающей машины — стучим из самой работы.
        """
        knocked = []

        with mock.patch.object(bake, "talk", lambda site, token, where, body: knocked.append(where)):
            beat = bake.Beat("https://knt-mipt.ru", "OqRZ7yTn3xK1", "подпись")
            beat()  # только что заведён — рано
            beat.last -= bake.BEAT + 1
            beat()
            beat()  # а этот снова рано: удары считаются от предыдущего, а не от вызова

        self.assertEqual(knocked, ["/intake/alive/"])

    def test_a_taken_job_gets_a_heartbeat_of_its_own(self):
        """Одна весточка на обе долгие фазы, и обязательно СВОЕГО задания: чужой токен
        продлевал бы срок работе, которую делает другая машина."""
        job = {"id": 3, "recipe": "lecture", "token": "подпись",
               "source": "https://r2/get", "name": "z.mkv"}

        with mock.patch.object(bake, "talk", lambda *a: {"job": job} if a[2] == "/intake/claim/" else {}), \
                mock.patch.object(bake, "say"), \
                mock.patch.object(bake, "fetch_source", return_value=1024) as fetched, \
                mock.patch.object(bake, "bake_lecture", side_effect=SystemExit("хватит")) as baked:
            bake.serve_once("https://knt-mipt.ru", "OqRZ7yTn3xK1", "ffmpeg",
                            bake.NVENC, "", spec.RECIPES)

        beat = fetched.call_args[0][2]
        self.assertIsInstance(beat, bake.Beat)
        self.assertEqual(beat.job, "подпись")
        self.assertIs(baked.call_args[0][-1], beat)  # и на выпечке та же самая

    def test_the_clock_ffmpeg_prints_becomes_seconds(self):
        """Ход выпечки ffmpeg сообщает временем записи, а не долей. В первые доли секунды
        там стоит «N/A» — на нём разбор в лоб и падал бы, ровно на первой строке."""
        self.assertAlmostEqual(bake.timecode("00:01:23.500000"), 83.5)
        self.assertAlmostEqual(bake.timecode("01:00:00.000000"), 3600.0)
        self.assertIsNone(bake.timecode("N/A"))
        self.assertIsNone(bake.timecode(""))

    def test_a_long_phase_says_how_far_it_got_and_how_much_is_left(self):
        """Тринадцать минут молчания на скачивании сырья неотличимы от зависшей пекарни,
        и человек снимает работу с полпути — так и вышло 10.09.2026. Отчёт нужен обеим
        долгим фазам, поэтому проверяем общий пульс: сколько сделано, сколько осталось.
        """
        said, beats = io.StringIO(), []
        with contextlib.redirect_stdout(said):
            pulse = bake.Pulse(600.0, unit=bake.spell, pace=bake.times)
            pulse.last = 0  # обычно первая строка ждёт своей секунды, а тесту ждать нечем
            bake.follow(["frame=12", "out_time=N/A", "out_time=00:00:00.000000",
                         "out_time=00:01:00.000000"], pulse, lambda: beats.append(1))

        self.assertTrue(beats)  # отсюда же уходит и весточка сайту: выпечка молчит долго

        line = said.getvalue()
        self.assertIn("1 мин 00 с из 10 мин 00 с", line)
        self.assertIn("10%", line)
        self.assertIn("×", line)  # обгон реального времени — по нему и видно, чем печём
        self.assertIn("осталось ~", line)
        # Ни «N/A», ни чужие ключи, ни нулевой ход строк не дают. Нуль тут не придирка:
        # ffmpeg печатает его первой же строкой, а «сколько осталось» на нуле сделанного
        # считается делением на него.
        self.assertEqual(line.count("\n"), 1)

    def test_the_download_reports_its_progress_and_still_writes_every_byte(self):
        """Фаза, из-за которой всё и затевалось: 3 ГБ на 4 МБ/с — тринадцать минут
        молчания. Своим циклом вместо `copyfileobj` она стала отчитываться — и заодно
        появилось место, где легко потерять кусок, поэтому проверяем и вес файла.
        """
        class Answer:
            headers = {"Content-Length": str(3 * 1024 ** 2)}

            def __init__(self):
                self.left = 3

            def read(self, size):
                self.left -= 1
                return b"x" * 1024 ** 2 if self.left >= 0 else b""

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        target = Path(tempfile.mkdtemp(prefix="bake-test-")) / "source.mkv"
        said, beats = io.StringIO(), []
        try:
            with mock.patch.object(bake.urllib.request, "urlopen", return_value=Answer()), \
                    mock.patch.object(bake, "QUIET", 0), contextlib.redirect_stdout(said):
                weight = bake.fetch_source("https://r2/get", target, lambda: beats.append(1))

            self.assertEqual(weight, 3 * 1024 ** 2)
            self.assertEqual(target.stat().st_size, 3 * 1024 ** 2)
        finally:
            shutil.rmtree(target.parent, ignore_errors=True)

        self.assertIn("из 3.0 МБ", said.getvalue())
        self.assertIn("МБ/с", said.getvalue())
        self.assertTrue(beats)  # 16 ГБ по плохому каналу — час, и сайту это надо знать

    def test_in_a_live_console_the_line_rewrites_itself(self):
        """Под tmux на пекарню смотрят живьём, и там строка обязана переписывать себя
        на месте: тринадцать минут скачивания оставили бы полторы сотни строк. А в логе
        `\\r` — это мусор, и туда пишется построчно и много реже."""
        said = LiveOut()
        with contextlib.redirect_stdout(said):
            pulse = bake.Pulse(1024 ** 3, pace=bake.rate)
            pulse.last = 0
            pulse.show(1024 ** 3 // 2)
            pulse.clear()

        self.assertIn("512.0 МБ из 1.00 ГБ", said.getvalue())
        self.assertIn("\r", said.getvalue())
        self.assertNotIn("\n", said.getvalue())  # иначе итог фазы въедет в хвост строки
        self.assertRegex(said.getvalue(), "\r {40,}\r$")  # и хвост за собой убрали
        self.assertEqual(pulse.every, bake.PULSE)

    def test_the_long_bake_is_watched_and_its_errors_still_come_back(self):
        """Ход спрашиваем у самого ffmpeg (`-progress`), и настройки эти общие — им место
        до всего прочего в командной строке. Ответ обязан остаться той же формы, что
        у `run`: вызывающий смотрит на код возврата и печатает человеку stderr."""
        fake = FakeFFmpeg(["out_time=00:00:05.000000", "progress=end"],
                          code=1, noise="Invalid pixel format\n")
        args = ["ffmpeg", "-i", "z.mkv", "out.m3u8"]

        with mock.patch.object(bake.subprocess, "Popen", fake), \
                contextlib.redirect_stdout(io.StringIO()):
            done = bake.watch(args, 600.0)

        self.assertEqual(fake.args[:4], ["ffmpeg", "-progress", "pipe:1", "-nostats"])
        self.assertEqual(fake.args[4:], ["-i", "z.mkv", "out.m3u8"])
        self.assertEqual(done.returncode, 1)
        self.assertIn("Invalid pixel format", done.stderr)

    def test_the_long_bake_goes_through_the_watcher(self):
        """Дорогая фаза ровно одна, и ход спрашивают у неё. Вернись она к безмолвному
        `run`, двадцать минут выпечки снова стали бы неотличимы от зависшей пекарни."""
        about = {"height": 1080, "fps": 60.0, "duration": 7200.0, "audio": True}
        out = Path(tempfile.mkdtemp(prefix="bake-test-"))
        try:
            with mock.patch.object(bake, "watch") as watched:
                bake.encode_ladder("ffmpeg", Path("z.mkv"), out, spec.RECIPES["lecture"],
                                   [1080, 720], about, "libx264", 26, "")
        finally:
            shutil.rmtree(out, ignore_errors=True)

        asked, seconds, _beat = watched.call_args[0]
        self.assertEqual(seconds, 7200.0)  # длительность записи, иначе процент не тот
        self.assertEqual(asked[0], "ffmpeg")

    def test_an_interrupted_bake_takes_the_encoder_down_with_it(self):
        """`subprocess.run` убивал ребёнка на любой ошибке сам, а с `Popen` это наша
        забота, и сигнал приходит только питону. Брошенный ffmpeg грел бы карту до утра —
        и писал бы в каталог, который пекарня уже считает убранным.
        """
        def lines():
            yield "out_time=00:00:05.000000"
            raise KeyboardInterrupt

        fake = FakeFFmpeg(lines())

        with mock.patch.object(bake.subprocess, "Popen", fake), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(KeyboardInterrupt):
            bake.watch(["ffmpeg", "-i", "z.mkv"], 600.0)

        self.assertTrue(fake.killed)


def made_manifest(duration=600.0, segments=None):
    # Число сегментов по умолчанию считаем от длительности: на этом сходстве и держится
    # проверка «заливка не оборвалась», и расходиться в тесте ему незачем.
    segments = ceil(duration / spec.RECIPES["lecture"].segment) if segments is None else segments
    return {
        "duration": duration, "poster": POSTER, "poster_bytes": 1024, "master": MASTER,
        "renditions": [
            {"height": h, "width": h * 16 // 9, "segments": segments, "bytes": 10 ** 7,
             "playlist": f"{i}/index.m3u8", "init": f"init_{i}.mp4"}
            for i, h in enumerate((1080, 720))
        ],
    }


def stored(prefix, manifest):
    """Что окажется в хранилище, если пекарня зальёт всё обещанное."""
    keys = {f"{prefix}/{MASTER}", f"{prefix}/{POSTER}"}
    for index, rendition in enumerate(manifest["renditions"]):
        keys.add(f"{prefix}/{rendition['playlist']}")
        keys.add(f"{prefix}/{index}/{rendition['init']}")
        keys |= {f"{prefix}/{index}/seg{n:05d}.m4s" for n in range(rendition["segments"])}
    return keys


@override_settings(INTAKE_TOKEN=TOKEN)
class QueueTests(TestCase):
    """Очередь: пекарня приходит сама, берёт задание и отчитывается."""

    def setUp(self):
        self.job = MediaJob.objects.create(recipe="lecture", source="uploads/abc/zapis.mkv")
        self.signed = mock.patch("intake.views.sign_download", return_value="https://r2/get")
        self.signed.start()
        self.addCleanup(self.signed.stop)
        self.put = mock.patch("intake.views.sign_put", side_effect=lambda key: f"https://r2/{key}")
        self.put.start()
        self.addCleanup(self.put.stop)

    def call(self, where, **body):
        return self.client.post(
            reverse(where), json.dumps(body), content_type="application/json",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    def claim(self):
        return self.call("intake_claim", worker="дом").json()["job"]

    def test_a_bakery_gets_work_and_a_link_to_the_source(self):
        job = self.claim()

        self.assertEqual(job["recipe"], "lecture")
        self.assertEqual(job["source"], "https://r2/get")
        self.assertEqual(job["name"], "zapis.mkv")

    def test_the_same_job_is_not_handed_out_twice(self):
        """Две пекарни, пришедшие разом, не должны получить одну лекцию."""
        self.assertIsNotNone(self.claim())
        self.assertIsNone(self.claim())

    def test_a_job_nobody_finished_comes_back_to_the_queue(self):
        """Машина упала или её выключили — иначе лекция висела бы вечно."""
        self.claim()
        MediaJob.objects.update(claimed_at=timezone.now() - timedelta(seconds=CLAIM_TIMEOUT + 60))

        again = self.claim()

        self.assertIsNotNone(again)
        self.assertEqual(MediaJob.objects.get().attempts, 2)

    def test_an_empty_queue_says_so(self):
        MediaJob.objects.all().delete()

        self.assertIsNone(self.claim())

    def test_a_stranger_is_not_let_near_the_queue(self):
        answer = self.client.post(reverse("intake_claim"), "{}", content_type="application/json")

        self.assertEqual(answer.status_code, 401)

    def test_a_forged_job_token_closes_nothing(self):
        """Номер задания приходит от пекарни: без подписи она закрыла бы чужое."""
        self.claim()
        forged = signing.dumps(self.job.pk, salt="не наша соль")

        self.assertEqual(self.call("intake_fail", token=forged, error="ой").status_code, 409)

    def test_a_manifest_off_spec_is_refused_before_a_single_piece_is_uploaded(self):
        """Отвергнуть описание дешевле, чем принять две тысячи кусков и обнаружить,
        что дорожка не та."""
        token = self.claim()["token"]
        wrong = made_manifest()
        wrong["renditions"][0]["height"] = 1440

        answer = self.call("intake_plan", token=token, manifest=wrong)

        self.assertEqual(answer.status_code, 400)
        self.assertIn("1440p", answer.json()["error"])

    def test_a_good_manifest_gets_a_folder(self):
        token = self.claim()["token"]

        prefix = self.call("intake_plan", token=token, manifest=made_manifest()).json()["prefix"]

        self.assertTrue(prefix.startswith("lectures/"))
        self.assertEqual(MediaJob.objects.get().prefix, prefix)

    def test_a_working_machine_does_not_have_its_job_taken_away(self):
        """Срок идёт от последней вести, а не от захвата. Иначе долгая работа — 16 ГБ
        сырья по плохому каналу, потом выпечка — уезжала бы к другой машине прямо
        из-под работающей, и обе делали бы одно и то же (так и вышло с #210 10.09.2026).
        """
        token = self.claim()["token"]
        MediaJob.objects.update(claimed_at=timezone.now() - timedelta(seconds=CLAIM_TIMEOUT + 60))

        self.assertEqual(self.call("intake_alive", token=token).status_code, 200)
        self.assertIsNone(self.claim())  # час прошёл, но машина сказала «жива»

    def test_a_machine_that_says_nothing_still_loses_the_job(self):
        """Обратная половина того же: молчание дольше срока — и задание свободно.
        Без неё упавшая пекарня заморозила бы лекцию навсегда."""
        self.claim()
        MediaJob.objects.update(claimed_at=timezone.now() - timedelta(seconds=CLAIM_TIMEOUT + 60))

        self.assertIsNotNone(self.claim())

    def test_the_replaced_machine_cannot_hold_the_job_by_beating(self):
        """Задание уже передали — значит, старая пекарня не хозяйка, и продлевать ей
        нечего. Иначе она удержала бы работу, которую в эту минуту делает другая."""
        token = self.claim()["token"]
        MediaJob.objects.update(claimed_at=timezone.now() - timedelta(seconds=CLAIM_TIMEOUT + 60))
        self.claim()

        self.assertEqual(self.call("intake_alive", token=token).status_code, 409)

    def test_handing_in_the_manifest_counts_as_a_word(self):
        """Правило одно на все ручки: всякое слово пекарни продлевает срок. Манифест
        приходит сразу после выпечки — самой долгой фазы, — и молчание перед ним уже
        могло почти исчерпать час."""
        token = self.claim()["token"]
        MediaJob.objects.update(claimed_at=timezone.now() - timedelta(seconds=CLAIM_TIMEOUT + 60))

        self.call("intake_plan", token=token, manifest=made_manifest())

        self.assertIsNone(self.claim())

    def test_asking_for_links_counts_as_a_word(self):
        """Заливка кусков идёт порциями по 200 и длится столько, сколько отдаст канал.
        Отдельная весточка тут не нужна — сама просьба о ссылках и есть весточка."""
        token = self.claim()["token"]
        self.call("intake_plan", token=token, manifest=made_manifest())
        MediaJob.objects.update(claimed_at=timezone.now() - timedelta(seconds=CLAIM_TIMEOUT + 60))

        self.assertEqual(self.call("intake_sign", token=token, names=["0/seg00000.m4s"]).status_code, 200)
        self.assertIsNone(self.claim())

    def test_links_are_signed_under_the_jobs_own_folder(self):
        token = self.claim()["token"]
        prefix = self.call("intake_plan", token=token, manifest=made_manifest()).json()["prefix"]

        urls = self.call("intake_sign", token=token, names=["0/seg00000.m4s"]).json()["urls"]

        self.assertIn(f"{prefix}/0/seg00000.m4s", urls["0/seg00000.m4s"])

    def test_the_bakery_is_told_what_type_to_declare(self):
        """Тип записывается в сам объект хранилища и потом уходит браузеру, а на попадании
        в кеш побеждает именно он. Пекарня раньше не объявляла никакого, и urllib
        подставлял свой — куски лекций лежали в R2 как `x-www-form-urlencoded`.
        Угадывать тип на её стороне нельзя: mimetypes на Windows читает реестр.
        """
        token = self.claim()["token"]
        self.call("intake_plan", token=token, manifest=made_manifest())

        types = self.call(
            "intake_sign", token=token, names=["0/seg00000.m4s", "master.m3u8"],
        ).json()["types"]

        self.assertEqual(types["0/seg00000.m4s"], "video/iso.segment")
        self.assertEqual(types["master.m3u8"], "application/vnd.apple.mpegurl")

    def test_climbing_out_of_the_folder_is_refused(self):
        token = self.claim()["token"]
        self.call("intake_plan", token=token, manifest=made_manifest())

        answer = self.call("intake_sign", token=token, names=["../../secret/key"])

        self.assertEqual(answer.status_code, 400)

    def test_a_broken_off_upload_is_not_accepted(self):
        """Без этой проверки лекция открылась бы наполовину."""
        token = self.claim()["token"]
        manifest = made_manifest()
        prefix = self.call("intake_plan", token=token, manifest=manifest).json()["prefix"]
        half = set(list(stored(prefix, manifest))[:50])

        with mock.patch("intake.views.under", return_value=half | {f"{prefix}/{MASTER}", f"{prefix}/{POSTER}"}):
            answer = self.call("intake_commit", token=token)

        self.assertEqual(answer.status_code, 400)
        self.assertIn("оборвалась", answer.json()["error"])

    def test_a_full_upload_closes_the_job(self):
        lecture = make_lecture()
        MediaJob.objects.update(lecture=lecture)
        token = self.claim()["token"]
        manifest = made_manifest(duration=3661)
        prefix = self.call("intake_plan", token=token, manifest=manifest).json()["prefix"]

        with mock.patch("intake.views.under", return_value=stored(prefix, manifest)), \
                mock.patch("intake.tasks.drop_prefix") as swept, \
                self.captureOnCommitCallbacks(execute=True):  # уборка ждёт коммита
            answer = self.call("intake_commit", token=token)

        lecture.refresh_from_db()
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(lecture.prefix, prefix)
        self.assertEqual(lecture.duration, 3661)
        self.assertEqual(MediaJob.objects.get().status, MediaJob.Status.DONE)
        # Сырьё больше не нужно: гигабайты, из которых уже всё взяли.
        swept.assert_called_once_with("uploads/abc")

    def test_a_second_attempt_sweeps_away_what_the_first_one_uploaded(self):
        """Задание вернулось в очередь после упавшей пекарни. Папка у второй попытки
        новая (класть свежие куски поверх недолитых — значит собрать набор из двух
        выпечек), а недолитое надо СНЯТЬ: иначе каждая осечка оставляла бы в бакете
        по гигабайту, которого потом ничем не найти."""
        first = self.call("intake_plan", token=self.claim()["token"],
                          manifest=made_manifest()).json()["prefix"]
        MediaJob.objects.update(claimed_at=timezone.now() - timedelta(seconds=CLAIM_TIMEOUT + 60))

        with mock.patch("lectorium.tasks.drop_prefix") as swept, \
                self.captureOnCommitCallbacks(execute=True):
            second = self.call("intake_plan", token=self.claim()["token"],
                               manifest=made_manifest()).json()["prefix"]

        self.assertNotEqual(second, first)
        swept.assert_called_once_with(first)

    def test_rebaking_a_lecture_sweeps_away_its_previous_set(self):
        """Задание вернули в очередь руками из админки. Лекция переезжает на новую
        папку, и старый набор остаётся никому не нужным."""
        lecture = make_lecture()
        lecture.prefix = "lectures/старый"
        lecture.save(update_fields=["prefix"])
        MediaJob.objects.update(lecture=lecture)
        token = self.claim()["token"]
        manifest = made_manifest()
        prefix = self.call("intake_plan", token=token, manifest=manifest).json()["prefix"]

        with mock.patch("intake.views.under", return_value=stored(prefix, manifest)), \
                mock.patch("intake.tasks.drop_prefix"), \
                mock.patch("lectorium.tasks.drop_prefix") as swept, \
                self.captureOnCommitCallbacks(execute=True):
            self.call("intake_commit", token=token)

        lecture.refresh_from_db()
        self.assertEqual(lecture.prefix, prefix)
        swept.assert_called_once_with("lectures/старый")

    def test_a_bakery_stopped_by_hand_gives_the_job_straight_back(self):
        """Ctrl+C на пекарне — не отказ: печь лекцию по-прежнему можно, просто некому
        в эту минуту. Без этой ручки запись висела бы «обрабатывается» весь
        CLAIM_TIMEOUT, пока сайт не сочтёт машину упавшей."""
        token = self.claim()["token"]

        answer = self.call("intake_release", token=token)

        job = MediaJob.objects.get()
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(job.status, MediaJob.Status.WAITING)
        self.assertIsNone(job.claimed_at)
        self.assertIsNotNone(self.claim())  # следующая берёт сразу, а не через час

    def test_the_stopped_bakery_cannot_come_back_to_its_job(self):
        """Задание к тому времени могла взять другая машина, и доделавшая своё первая
        снесла бы её папку. Токен умирает вместе с возвратом в очередь."""
        token = self.claim()["token"]
        self.call("intake_release", token=token)

        answer = self.call("intake_plan", token=token, manifest=made_manifest())

        self.assertEqual(answer.status_code, 409)

    def test_a_failure_reaches_the_person(self):
        """Иначе лекция висит «обрабатывается» вечно, и никто не знает почему."""
        token = self.claim()["token"]

        self.call("intake_fail", token=token, error="ffmpeg не справился")

        job = MediaJob.objects.get()
        self.assertEqual(job.status, MediaJob.Status.FAILED)
        self.assertEqual(job.note, "ffmpeg не справился")

    def test_the_worker_that_lost_the_job_cannot_touch_it_any_more(self):
        """Медленная пекарня не умерла — она просто молчала дольше часа, и задание
        к тому времени отдали другой. Доделав своё, она снесла бы папку сменщицы."""
        first = self.claim()["token"]
        MediaJob.objects.update(claimed_at=timezone.now() - timedelta(seconds=CLAIM_TIMEOUT + 60))
        second = self.claim()["token"]

        self.assertNotEqual(first, second)
        self.assertEqual(self.call("intake_plan", token=first, manifest=made_manifest()).status_code, 409)
        self.assertEqual(self.call("intake_plan", token=second, manifest=made_manifest()).status_code, 200)

    def test_a_job_handed_back_over_and_over_is_given_up(self):
        """Так и было 30.09.2026: пекарня брала задание, тут же спотыкалась, возвращала
        его — и брала снова. 32767 кругов, потом счётчик переполнился, и `claim` стал
        падать пятисоткой у всех. Такое задание сломано само, и человеку надо это сказать."""
        for _ in range(MAX_TRIES):
            self.call("intake_release", token=self.claim()["token"])

        self.assertIsNone(self.claim())
        job = MediaJob.objects.get()
        self.assertEqual(job.status, MediaJob.Status.FAILED)
        self.assertIn(f"{MAX_TRIES} раз", job.note)

    def test_a_worn_out_job_does_not_block_the_queue(self):
        """Очередь идёт от старых к новым, и сломанное задание стоит в самой её голове:
        не пропусти его — и новые записи не пеклись бы никогда."""
        MediaJob.objects.update(tries=MAX_TRIES)
        fresh = MediaJob.objects.create(recipe="lecture", source="uploads/def/novaya.mkv")

        self.assertEqual(self.claim()["id"], fresh.pk)
        self.assertEqual(MediaJob.objects.get(pk=self.job.pk).status, MediaJob.Status.FAILED)

    def test_a_person_putting_the_job_back_gives_it_a_fresh_run(self):
        """Иначе закрытое по пределу задание, возвращённое из админки, закрылось бы снова
        на первом же `claim`. Номер попытки при этом не сбрасывается — он живёт в токене,
        и сброс воскресил бы токены прошлых пекарен."""
        MediaJob.objects.update(status=MediaJob.Status.FAILED, tries=MAX_TRIES, attempts=MAX_TRIES)
        job = MediaJob.objects.get()
        job.status = MediaJob.Status.WAITING

        MediaJobAdmin(MediaJob, admin.site).save_model(None, job, mock.Mock(changed_data=["status"]), True)

        self.assertIsNotNone(self.claim())
        job.refresh_from_db()
        self.assertEqual((job.tries, job.attempts), (1, MAX_TRIES + 1))


@override_settings(INTAKE_TOKEN=TOKEN)
class LeftoverTests(TestCase):
    """Что остаётся в хранилище после каждого способа не довести выпечку до конца.

    Ключей от бакета у сайта много, а памяти о них — ровно одна строка в базе: пропала
    она, и папку с гигабайтом кусков потом не найти ничем. Поэтому каждый выход из
    выпечки проверяется отдельно.
    """

    def setUp(self):
        self.lecture = make_lecture()
        self.job = MediaJob.objects.create(
            recipe="lecture", source="uploads/abc/zapis.mkv", lecture=self.lecture,
        )
        for name, answer in (("sign_download", "https://r2/get"), ("sign_put", "https://r2/put")):
            patch = mock.patch(f"intake.views.{name}", return_value=answer)
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, where, **body):
        return self.client.post(
            reverse(where), json.dumps(body), content_type="application/json",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    def claim(self):
        return self.call("intake_claim", worker="дом").json()["job"]["token"]

    def bake(self):
        """Полный круг: взять задание, испечь, залить, закрыть. Возвращает папку набора."""
        token = self.claim()
        manifest = made_manifest()
        prefix = self.call("intake_plan", token=token, manifest=manifest).json()["prefix"]
        with mock.patch("intake.views.under", return_value=stored(prefix, manifest)), \
                mock.patch("intake.tasks.drop_prefix"), \
                mock.patch("lectorium.tasks.drop_prefix"), \
                self.captureOnCommitCallbacks(execute=True):
            self.call("intake_commit", token=token)
        return prefix

    def swept(self):
        """Что уедет из хранилища за время блока. Возвращает mock, накопивший префиксы."""
        return mock.patch("lectorium.tasks.drop_prefix")

    def test_a_job_sent_back_to_the_queue_keeps_the_set_until_the_new_one_is_ready(self):
        """Задание вернули в очередь из админки, чтобы перепечь. Папка задания в этот
        момент — набор ОПУБЛИКОВАННОЙ лекции, и снять её значит погасить лекцию
        на всё время новой выпечки, а при осечке — навсегда."""
        live = self.bake()
        MediaJob.objects.update(status=MediaJob.Status.WAITING)

        with self.swept() as swept, self.captureOnCommitCallbacks(execute=True):
            self.call("intake_plan", token=self.claim(), manifest=made_manifest())

        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.prefix, live)
        swept.assert_not_called()

    def test_the_previous_set_goes_only_when_the_new_one_has_taken_its_place(self):
        """Продолжение предыдущего: как только лекция переехала, старый набор не нужен."""
        live = self.bake()
        MediaJob.objects.update(status=MediaJob.Status.WAITING)

        with self.swept() as swept, self.captureOnCommitCallbacks(execute=True):
            fresh = self.bake()

        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.prefix, fresh)
        self.assertEqual([call.args[0] for call in swept.call_args_list], [live])

    def test_a_failure_sweeps_what_had_already_been_uploaded(self):
        """Самый обычный отказ — оборвавшаяся ЗАЛИВКА, то есть папка уже с гигабайтом
        кусков. Повтора может и не быть, и тогда ждать его нечего."""
        token = self.claim()
        prefix = self.call("intake_plan", token=token, manifest=made_manifest()).json()["prefix"]

        with self.swept() as swept, self.captureOnCommitCallbacks(execute=True):
            self.call("intake_fail", token=token, error="связь оборвалась")

        swept.assert_called_once_with(prefix)
        self.assertEqual(MediaJob.objects.get().prefix, "")

    def test_a_stopped_bakery_sweeps_what_it_had_already_uploaded(self):
        """Останавливают чаще всего на заливке — она дольше всего. Папка к этой минуте
        уже с гигабайтом кусков, а следующая выпечка заведёт свою: брошенную потом
        не найти ничем, ночная уборка занятые заданием папки не трогает."""
        token = self.claim()
        prefix = self.call("intake_plan", token=token, manifest=made_manifest()).json()["prefix"]

        with self.swept() as swept, self.captureOnCommitCallbacks(execute=True):
            self.call("intake_release", token=token)

        swept.assert_called_once_with(prefix)
        self.assertEqual(MediaJob.objects.get().prefix, "")

    def test_a_stopped_bakery_does_not_touch_the_lecture_that_is_still_playing(self):
        """Перепечка, прерванная руками. Папка задания в этот момент — набор
        ОПУБЛИКОВАННОЙ лекции, и снять её значило бы погасить лекцию насовсем."""
        live = self.bake()
        MediaJob.objects.update(status=MediaJob.Status.WAITING)
        token = self.claim()

        with self.swept() as swept, self.captureOnCommitCallbacks(execute=True):
            self.call("intake_release", token=token)

        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.prefix, live)
        swept.assert_not_called()

    def test_a_failed_bake_does_not_touch_the_lecture_that_is_still_playing(self):
        live = self.bake()
        MediaJob.objects.update(status=MediaJob.Status.WAITING)
        token = self.claim()

        with self.swept() as swept, self.captureOnCommitCallbacks(execute=True):
            self.call("intake_fail", token=token, error="ffmpeg упал")

        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.prefix, live)
        swept.assert_not_called()

    def test_deleting_a_lecture_mid_bake_takes_the_leftovers_with_it(self):
        """Задание уезжает каскадом, и вместе с ним пропадает единственная память
        о недолитой папке и о сырье в десятки гигабайт."""
        token = self.claim()
        prefix = self.call("intake_plan", token=token, manifest=made_manifest()).json()["prefix"]

        with self.swept() as folder, mock.patch("intake.tasks.drop_prefix") as source, \
                self.captureOnCommitCallbacks(execute=True):
            self.lecture.delete()

        folder.assert_called_once_with(prefix)
        source.assert_called_once_with("uploads/abc")

    def test_deleting_a_closed_job_leaves_the_lecture_alone(self):
        """У готового задания папка принадлежит лекции, а сырьё снял `commit`."""
        live = self.bake()

        with self.swept() as folder, mock.patch("intake.tasks.drop_prefix") as source, \
                self.captureOnCommitCallbacks(execute=True):
            MediaJob.objects.get().delete()

        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.prefix, live)
        folder.assert_not_called()
        source.assert_not_called()

    def test_source_that_is_also_a_material_file_is_not_taken_away(self):
        """Токен загрузки не одноразовый: тот же файл можно сдать и в лекторий,
        и приложением к материалу. Тогда у ключа есть второй хозяин."""
        material = Material.objects.create(
            title="Конспект", subject=self.lecture.playlist.subject,
            uploader=self.lecture.playlist.uploader,
        )
        File.objects.create(material=material, name="запись", file="uploads/abc/zapis.mkv", size=1)

        with mock.patch("intake.tasks.drop_prefix") as swept:
            drop_source("uploads/abc/zapis.mkv")

        swept.assert_not_called()


class DjangoFreeTests(SimpleTestCase):
    """`spec` и `mp4` уезжают с пекарней на чужую машину, где Django нет вовсе.

    Случайный `from django...` в них ничего не сломает здесь — и обнаружится на той
    машине, у человека, который просто хотел испечь фон. Поэтому проверяем отсюда.
    """

    def test_the_bakery_half_imports_without_django(self):
        code = "import intake.spec, intake.mp4, sys; print('django' in sys.modules)"
        env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=settings.BASE_DIR,
            capture_output=True, text=True, encoding="utf-8", env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")
