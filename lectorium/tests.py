from unittest import mock
from uuid import uuid4

from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from attachments.storage import file_storage
from attachments.uploads import adopt_token
from core.models import Subject
from intake.models import MediaJob
from users.models import User

from .models import Lecture, Playlist


def make_user(email="u@t.local", surname="Иванов", **extra):
    return User.objects.create_user(
        email=email, name="Иван", surname=surname, password="pass12345",
        must_change_password=False, **extra,
    )


def make_set(prefix):
    """Испечённый набор в хранилище: манифест, дорожка, сегмент, обложка."""
    storage = file_storage()
    for name, body in [
        ("master.m3u8", b"#EXTM3U\n"), ("poster.jpg", b"jpg"),
        ("0/index.m3u8", b"#EXTM3U\n"), ("0/seg00000.m4s", b"segment"),
        ("0/init_0.mp4", b"init"),
    ]:
        storage.save(f"{prefix}/{name}", ContentFile(body))


class LectoriumTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.subject = Subject.objects.create(name="Физика", dative="физике", accusative="физику")
        cls.author = make_user("a@t.local")
        cls.stranger = make_user("s@t.local", surname="Петров")
        cls.moderator = make_user("m@t.local", surname="Сидоров")
        cls.moderator.user_permissions.add(Permission.objects.get(codename="change_playlist"))

    def make_playlist(self, title="Механика", status=Playlist.Status.APPROVED, lectures=1):
        playlist = Playlist.objects.create(
            title=title, subject=self.subject, uploader=self.author, status=status,
        )
        for number in range(lectures):
            Lecture.objects.create(
                playlist=playlist, title=f"Лекция {number + 1}", order=number,
                prefix=f"lectures/{title}-{number}", duration=3661,
            )
        return playlist


class VisibilityTests(LectoriumTests):
    """Непроверенный плейлист видят только автор и модерация — как у материалов."""

    def titles(self, who):
        self.client.force_login(who)
        return [p.title for p in self.client.get(reverse("playlist_list")).context["playlists"]]

    def setUp(self):
        self.open = self.make_playlist("Открытый")
        self.waiting = self.make_playlist("Ждёт", status=Playlist.Status.PENDING)

    def test_a_stranger_sees_only_what_is_published(self):
        self.assertEqual(self.titles(self.stranger), ["Открытый"])

    def test_the_author_sees_his_own_unpublished(self):
        self.assertCountEqual(self.titles(self.author), ["Открытый", "Ждёт"])

    def test_moderation_sees_everything(self):
        self.assertCountEqual(self.titles(self.moderator), ["Открытый", "Ждёт"])

    def test_someone_elses_unpublished_is_not_found(self):
        self.client.force_login(self.stranger)

        self.assertEqual(self.client.get(self.waiting.get_absolute_url()).status_code, 404)


class DetailTests(LectoriumTests):
    """Страница курса: плеер на выбранной записи, остальные — списком рядом."""

    def setUp(self):
        self.playlist = self.make_playlist(lectures=3)
        self.client.force_login(self.author)

    def open(self, **params):
        return self.client.get(self.playlist.get_absolute_url(), params)

    def test_the_first_lecture_plays_by_default(self):
        page = self.open()

        self.assertEqual(page.context["lecture"], self.playlist.lectures.first())
        self.assertIn("lecturePlayer", page.content.decode())

    def test_a_chosen_lecture_plays(self):
        third = self.playlist.lectures.all()[2]

        self.assertEqual(self.open(lecture=third.pk).context["lecture"], third)

    def test_a_lecture_from_another_playlist_is_ignored(self):
        """Номер приходит из адреса, и подставить туда можно что угодно. Показываем
        первую свою, а не чужую запись."""
        alien = self.make_playlist("Чужой").lectures.first()

        self.assertEqual(self.open(lecture=alien.pk).context["lecture"], self.playlist.lectures.first())

    def test_nonsense_in_the_address_does_not_break_the_page(self):
        self.assertEqual(self.open(lecture="ой").status_code, 200)

    def test_an_empty_playlist_says_so_instead_of_showing_a_player(self):
        empty = self.make_playlist("Пустой", lectures=0)
        page = self.client.get(empty.get_absolute_url())

        self.assertIsNone(page.context["lecture"])
        self.assertNotIn("lecturePlayer", page.content.decode())

    def test_a_record_still_in_the_oven_does_not_open_a_player(self):
        """Ссылку на запись пересылают, а обработка идёт час. Папки набора у неё ещё нет,
        и плеер получил бы адрес в никуда вместо честного «обрабатывается»."""
        waiting = Lecture.objects.create(playlist=self.playlist, title="Свежая", order=9, prefix="")

        page = self.open(lecture=waiting.pk)

        self.assertEqual(page.context["lecture"], self.playlist.lectures.first())
        self.assertContains(page, "обрабатывается")

    def test_a_playlist_of_nothing_but_unbaked_records_shows_no_player(self):
        fresh = self.make_playlist("Свежий", lectures=0)
        Lecture.objects.create(playlist=fresh, title="Ждёт", order=0, prefix="")

        page = self.client.get(fresh.get_absolute_url())

        self.assertIsNone(page.context["lecture"])
        self.assertContains(page, "обрабатываются")


class ReviewTests(LectoriumTests):
    def setUp(self):
        self.playlist = self.make_playlist(status=Playlist.Status.PENDING)

    def decide(self, who, **data):
        self.client.force_login(who)
        return self.client.post(reverse("playlist_review", args=[self.playlist.pk]), data)

    def test_a_moderator_publishes(self):
        self.decide(self.moderator, decision="approve")
        self.playlist.refresh_from_db()

        self.assertTrue(self.playlist.is_published)
        self.assertEqual(self.playlist.reviewed_by, self.moderator)

    def test_a_rejection_carries_the_reason_to_the_author(self):
        self.decide(self.moderator, decision="reject", note="Звука нет")
        self.playlist.refresh_from_db()

        self.assertEqual(self.playlist.status, Playlist.Status.REJECTED)
        self.assertEqual(self.playlist.review_note, "Звука нет")

    def test_nobody_else_decides(self):
        self.assertEqual(self.decide(self.author, decision="approve").status_code, 403)

    def test_it_waits_in_the_common_queue(self):
        """Очередь одна на весь сайт: модератор не должен обходить разделы по очереди."""
        self.client.force_login(self.moderator)
        page = self.client.get(reverse("review_queue")).content.decode()

        self.assertIn(self.playlist.title, page)


class SegmentCleanupTests(LectoriumTests):
    """Набор снимается вместе с лекцией: `post_delete` в attachments трогает только
    файловые ПОЛЯ, а тут их нет — есть папка на тысячи сегментов."""

    def left(self, prefix):
        """Сколько всего осталось под префиксом. Папки нет вовсе — значит ноль:
        на диске мы убираем и опустевшие каталоги, в бакете их не бывает."""
        try:
            folders, files = file_storage().listdir(prefix)
        except (FileNotFoundError, NotADirectoryError):
            return 0
        return len(files) + len(folders)

    def test_deleting_a_lecture_clears_its_folder(self):
        lecture = self.make_playlist(lectures=1).lectures.first()
        make_set(lecture.prefix)
        self.assertTrue(self.left(lecture.prefix))

        with self.captureOnCommitCallbacks(execute=True):
            lecture.delete()

        self.assertFalse(self.left(lecture.prefix))

    def test_deleting_a_playlist_takes_every_lecture_with_it(self):
        playlist = self.make_playlist(lectures=3)
        prefixes = [one.prefix for one in playlist.lectures.all()]
        for prefix in prefixes:
            make_set(prefix)

        with self.captureOnCommitCallbacks(execute=True):
            playlist.delete()

        self.assertEqual([self.left(prefix) for prefix in prefixes], [0, 0, 0])

    def test_files_stay_while_the_lecture_does(self):
        """Уборка висит на удалении, а не на сохранении: правка названия не должна
        уносить набор."""
        lecture = self.make_playlist(lectures=1).lectures.first()
        make_set(lecture.prefix)

        with self.captureOnCommitCallbacks(execute=True):
            lecture.title = "Другое имя"
            lecture.save()

        self.assertTrue(self.left(lecture.prefix))


class LectureTests(LectoriumTests):
    def test_the_length_is_shown_the_way_people_say_it(self):
        lecture = self.make_playlist(lectures=1).lectures.first()

        self.assertEqual(lecture.human_duration(), "1:01:01")
        lecture.duration = 750
        self.assertEqual(lecture.human_duration(), "12:30")

    def test_the_manifest_and_the_poster_are_found_by_the_prefix(self):
        lecture = self.make_playlist(lectures=1).lectures.first()

        self.assertTrue(lecture.manifest_key.endswith("/master.m3u8"))
        self.assertTrue(lecture.poster_key.endswith("/poster.jpg"))
        self.assertIn("/hls/", lecture.manifest_url())


class SubmitTests(LectoriumTests):
    """Сдача записи: файл уже в хранилище, здесь заводятся лекция и задание."""

    def setUp(self):
        self.keeper = make_user("k@t.local", surname="Хранов")
        self.keeper.user_permissions.add(Permission.objects.get(codename="add_playlist"))
        self.keeper = User.objects.get(pk=self.keeper.pk)  # права кешируются на объекте
        self.playlist = Playlist.objects.create(
            title="Механика", subject=self.subject, uploader=self.keeper,
            status=Playlist.Status.APPROVED,
        )

    def source(self, body=b"raw-video"):
        """Сырьё так, как оно лежит после прямой загрузки: файл в хранилище плюс токен
        на него. Класть по-настоящему обязательно — вьюха спрашивает у хранилища,
        доехало ли, и подписью одной не удовлетворяется."""
        key = f"uploads/{uuid4().hex}/zapis.mkv"
        file_storage().save(key, ContentFile(body))
        return adopt_token(key, "zapis.mkv")

    def submit(self, who=None, title="Первая", token=None):
        self.client.force_login(who or self.keeper)
        return self.client.post(reverse("lecture_add", args=[self.playlist.pk]), {
            "title": title, "uploaded": self.source() if token is None else token,
        })

    def test_a_record_becomes_a_lecture_and_a_job(self):
        self.submit()

        lecture = self.playlist.lectures.get()
        self.assertEqual(lecture.title, "Первая")
        self.assertEqual(lecture.prefix, "")  # появится, когда пекарня отчитается
        self.assertEqual(lecture.job.status, MediaJob.Status.WAITING)
        self.assertTrue(lecture.job.source.startswith("uploads/"))

    def test_several_records_wait_in_line_together(self):
        """Пустой префикс у всех, кто ждёт очереди: уникальность не должна им мешать."""
        self.submit(title="Первая")
        self.submit(title="Вторая")

        self.assertEqual(self.playlist.lectures.count(), 2)

    def test_a_record_that_never_reached_the_bucket_is_refused(self):
        """Подпись честная, файла нет: браузер оборвался на середине заливки. Ловим тут,
        иначе задание встало бы в очередь и упало у пекарни через час — а человек
        всё это время считал бы, что дело сделано."""
        answer = self.submit(token=adopt_token("uploads/пусто/zapis.mkv", "zapis.mkv"))

        self.assertEqual(self.playlist.lectures.count(), 0)
        self.assertEqual(MediaJob.objects.count(), 0)
        # Причину человек должен увидеть: молчаливый отказ выглядит как «отправилось».
        self.assertTrue(any("не доехала" in str(one) for one in get_messages(answer.wsgi_request)))

    def test_a_record_heavier_than_the_limit_is_refused(self):
        """Размер в подписанной ссылке не участвует: браузер объявляет его до отправки,
        а положить по ссылке может сколько угодно. Правду знает только хранилище."""
        with mock.patch("lectorium.views.max_upload_size", return_value=4):
            self.submit()

        self.assertEqual(self.playlist.lectures.count(), 0)

    def test_a_record_without_a_file_is_refused(self):
        self.submit(token="")

        self.assertEqual(self.playlist.lectures.count(), 0)

    def test_a_forged_upload_token_is_refused(self):
        """Ключ приходит от браузера: без подписи он подсунул бы чужой файл."""
        self.submit(token="uploads/чужое/файл.mkv")

        self.assertEqual(self.playlist.lectures.count(), 0)

    def test_a_stranger_does_not_add_records(self):
        # Токен любой: право проверяется раньше, чем разбирается файл.
        self.assertEqual(self.submit(who=self.stranger, token="всё равно").status_code, 403)

    def test_someone_elses_unpublished_course_is_not_even_visible(self):
        """Не 403, а 404: постороннему незачем знать, что такой курс вообще есть."""
        hidden = Playlist.objects.create(title="Скрытый", subject=self.subject, uploader=self.keeper)
        self.client.force_login(self.stranger)

        answer = self.client.post(reverse("lecture_add", args=[hidden.pk]), {"title": "Ой"})

        self.assertEqual(answer.status_code, 404)


class PlaylistFormTests(LectoriumTests):
    def setUp(self):
        self.keeper = make_user("k@t.local", surname="Хранов")
        self.keeper.user_permissions.add(Permission.objects.get(codename="add_playlist"))
        self.keeper = User.objects.get(pk=self.keeper.pk)

    def create(self, who):
        self.client.force_login(who)
        return self.client.post(reverse("playlist_new"), {
            "title": "Механика", "subject": self.subject.pk, "year": 2026, "synopsis": "",
        })

    def test_whoever_has_the_right_starts_a_course(self):
        self.create(self.keeper)

        playlist = Playlist.objects.get()
        self.assertEqual(playlist.uploader, self.keeper)
        self.assertTrue(playlist.is_pending)  # курс уходит на проверку, как материал

    def test_a_stranger_does_not(self):
        """Лекции выкладывают не все подряд: печь их дорого."""
        self.assertEqual(self.create(self.stranger).status_code, 403)
        self.assertEqual(Playlist.objects.count(), 0)

    def test_editing_a_published_course_sends_it_back_to_review(self):
        """Иначе одобренное можно было бы тихо подменить."""
        playlist = self.make_playlist(status=Playlist.Status.APPROVED, lectures=0)
        playlist.uploader = self.keeper
        playlist.save()
        self.client.force_login(self.keeper)

        self.client.post(reverse("playlist_edit", args=[playlist.pk]), {
            "title": "Другое имя", "subject": self.subject.pk, "year": 2026, "synopsis": "",
        })

        playlist.refresh_from_db()
        self.assertTrue(playlist.is_pending)


@override_settings(BETA=True)
class BetaTests(LectoriumTests):
    """Раздел готов, а сдавать лекции ещё нечем — до тех пор он закрыт."""

    def test_the_section_is_shut_for_everyone_but_staff(self):
        self.client.force_login(self.stranger)

        self.assertEqual(self.client.get(reverse("playlist_list")).status_code, 403)

    def test_staff_can_look(self):
        self.client.force_login(make_user("st@t.local", is_staff=True))

        self.assertEqual(self.client.get(reverse("playlist_list")).status_code, 200)


class CheckPageTests(LectoriumTests):
    """Служебная страница: посмотреть набор HLS по ключу его манифеста. Набор бывает
    залит, но ещё не привязан к лекции, — а другого способа убедиться, что он играет, нет."""

    def setUp(self):
        self.key = file_storage().save("lectures/abc/master.m3u8", ContentFile(b"#EXTM3U\n"))

    def open(self, key=None, who=None):
        self.client.force_login(who or make_user("st@t.local", is_staff=True))
        return self.client.get(reverse("hls_check"), {"key": key} if key else {})

    def test_a_set_that_exists_gets_a_player(self):
        page = self.open(self.key).content.decode()

        self.assertIn("lecturePlayer", page)
        self.assertIn("/hls/", page)

    def test_a_missing_key_is_said_out_loud(self):
        """Иначе плеер молча показывал бы чёрный прямоугольник, и было бы непонятно,
        то ли выпечка не доехала, то ли раздача сломана."""
        self.assertIn("нет такого куска", self.open("lectures/net/master.m3u8").content.decode())

    def test_a_key_that_is_not_a_manifest_is_refused(self):
        self.assertIn("m3u8", self.open("lectures/abc/seg00000.m4s").content.decode())

    def test_an_empty_form_shows_no_player(self):
        self.assertNotIn("lecturePlayer", self.open().content.decode())

    def test_it_is_only_for_staff(self):
        self.assertEqual(self.open(self.key, who=self.stranger).status_code, 403)
