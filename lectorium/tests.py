from unittest import mock
from uuid import uuid4

from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from attachments.storage import file_storage
from attachments.uploads import adopt_token
from core.models import Subject, Term
from economy import rewards
from economy.services import wallet_of
from intake.models import MediaJob
from teachers.models import Teacher
from users.models import User

from .forms import PlaylistForm
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


class FilterTests(LectoriumTests):
    """Подбор курсов — тот же, что у материалов (core/filters.py), и проверяем его
    здесь отдельно: общий код легко сломать правкой ради одного из двух разделов."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.matan = Subject.objects.create(name="Матан", dative="матану", accusative="матан")
        cls.first = Term.objects.create(number=1)
        cls.second = Term.objects.create(number=2)
        cls.lector = Teacher.objects.create(surname="Цветкова", name="Анна", patronymic="Валерьевна")

    def setUp(self):
        self.algebra = self.make_playlist("Линейная алгебра")
        self.algebra.subject = self.matan
        self.algebra.save(update_fields=["subject"])
        self.algebra.terms.add(self.first)
        self.algebra.teachers.add(self.lector)

        self.mechanics = self.make_playlist("Механика")  # предмет Физика, второй семестр
        self.mechanics.terms.add(self.second)
        self.client.force_login(self.author)

    def get(self, **params):
        return self.client.get(reverse("playlist_list"), params)

    def titles(self, response):
        return [one.title for one in response.context["playlists"]]

    def options(self, response, name):
        return set(response.context["form"].fields[name].queryset.values_list("pk", flat=True))

    def test_courses_are_picked_by_subject_term_and_teacher(self):
        self.assertEqual(self.titles(self.get(subject=self.matan.pk)), ["Линейная алгебра"])
        self.assertEqual(self.titles(self.get(term=self.second.pk)), ["Механика"])
        self.assertEqual(self.titles(self.get(teacher=self.lector.pk)), ["Линейная алгебра"])

    def test_choosing_a_term_leaves_only_what_it_has(self):
        first = self.get(term=self.first.pk)

        self.assertEqual(self.options(first, "subject"), {self.matan.pk})
        self.assertEqual(self.options(first, "teacher"), {self.lector.pk})

    def test_a_filter_never_narrows_itself(self):
        # Иначе в списке осталось бы одно выбранное значение и сменить его было бы нечем.
        self.assertEqual(self.options(self.get(term=self.first.pk), "term"),
                         {self.first.pk, self.second.pk})

    def test_garbage_in_the_address_does_not_break_the_page(self):
        self.assertEqual(self.get(subject="нет", term="-1", teacher="ерунда").status_code, 200)

    def test_the_picked_filters_go_into_the_address(self):
        response = self.client.get(
            reverse("playlist_list"), {"subject": self.matan.pk, "term": ""},
            headers={"HX-Request": "true"},
        )

        self.assertIn(f"subject={self.matan.pk}", response["HX-Push-Url"])
        self.assertNotIn("term=", response["HX-Push-Url"])

    def test_a_card_carries_the_picked_filters_into_its_link(self):
        """По этой строке страница курса и узнаёт, куда возвращать по «Лекторий»."""
        response = self.get(term=self.first.pk)

        self.assertContains(response, f"{self.algebra.get_absolute_url()}?term={self.first.pk}")

    def test_the_course_page_returns_to_the_same_picking(self):
        page = self.client.get(self.algebra.get_absolute_url(), {"term": self.first.pk})

        self.assertEqual(page.context["back_url"], f"{reverse('playlist_list')}?term={self.first.pk}")
        # И запись открывается, не теряя подбора: иначе первый же клик стёр бы его.
        self.assertContains(page, f"?term={self.first.pk}&amp;lecture=")

    def test_a_course_with_two_teachers_counts_its_records_once(self):
        """Подбор по преподавателю — join по многие-ко-многим: без distinct курс насчитал
        бы себе вдвое больше записей, чем в нём есть."""
        self.algebra.teachers.add(Teacher.objects.create(surname="Иванов", name="Иван"))

        found = self.get(term=self.first.pk).context["playlists"][0]

        self.assertEqual(found.lectures_count, self.algebra.lectures.count())


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
        MediaJob.objects.create(
            recipe="lecture", source="uploads/a/z.mkv", lecture=waiting,
            status=MediaJob.Status.BAKING,
        )

        page = self.open(lecture=waiting.pk)

        self.assertEqual(page.context["lecture"], self.playlist.lectures.first())
        self.assertContains(page, "обрабатывается")

    def test_the_course_is_named_above_its_records(self):
        """Заголовок страницы показывает ОТКРЫТУЮ запись, и без этой строки непонятно,
        из какого она курса."""
        page = self.open()

        self.assertContains(page, self.playlist.title)
        self.assertContains(page, "3 записи")

    def test_every_ready_record_shows_its_own_frame(self):
        page = self.open()

        for one in self.playlist.lectures.all():
            self.assertContains(page, one.poster_url())

    def test_the_menu_lights_the_section_not_the_page(self):
        self.assertEqual(self.open().context["section"], "lectorium")

    def test_a_playlist_of_nothing_but_unbaked_records_shows_no_player(self):
        fresh = self.make_playlist("Свежий", lectures=0)
        Lecture.objects.create(playlist=fresh, title="Ждёт", order=0, prefix="")

        page = self.client.get(fresh.get_absolute_url())

        self.assertIsNone(page.context["lecture"])
        self.assertContains(page, "обрабатываются")


class ReactionTests(LectoriumTests):
    """Оценка у ЗАПИСИ, а не у курса: курс из двадцати лекций читается разного качества."""

    def setUp(self):
        self.playlist = self.make_playlist(lectures=2)
        self.lecture = self.playlist.lectures.first()
        self.client.force_login(self.stranger)

    def vote(self, which, lecture=None):
        return self.client.post(reverse(f"lecture_{which}", args=[(lecture or self.lecture).pk]))

    def test_the_buttons_are_shown_under_the_open_record(self):
        page = self.client.get(self.playlist.get_absolute_url())

        self.assertContains(page, "lecture-reactions")
        self.assertEqual(page.context["lecture"].likes, 0)

    def test_a_like_is_counted_and_can_be_taken_back(self):
        self.vote("like")
        self.assertEqual(self.lecture.liked_users.count(), 1)

        self.vote("like")  # повторный клик снимает голос
        self.assertEqual(self.lecture.liked_users.count(), 0)

    def test_a_dislike_moves_the_vote_instead_of_doubling_it(self):
        self.vote("like")

        self.vote("dislike")

        self.assertEqual(self.lecture.liked_users.count(), 0)
        self.assertEqual(self.lecture.disliked_users.count(), 1)

    def test_the_answer_carries_the_fresh_count(self):
        answer = self.vote("like")

        self.assertContains(answer, "lecture-reactions")
        self.assertContains(answer, "1")

    def test_votes_of_two_people_add_up(self):
        self.vote("like")
        self.client.force_login(self.author)
        self.vote("like")

        self.assertEqual(self.lecture.liked_users.count(), 2)

    def test_a_record_of_an_unchecked_course_cannot_be_voted_on(self):
        hidden = self.make_playlist("Черновик", status=Playlist.Status.PENDING).lectures.first()

        self.assertEqual(self.vote("like", hidden).status_code, 404)
        self.assertEqual(hidden.liked_users.count(), 0)


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

    def test_publishing_pays_the_author_and_the_moderator_right_away(self):
        """Начисление зовём тут же, а не ждём следующего входа автора: иначе он решил бы,
        что токенов не дали вовсе."""
        self.decide(self.moderator, decision="approve")

        self.assertEqual(wallet_of(self.author).balance, rewards.WELCOME + rewards.PLAYLIST)
        self.assertEqual(wallet_of(self.moderator).balance, rewards.WELCOME + rewards.MODERATION)

    def test_a_rejected_course_is_not_paid_for(self):
        self.decide(self.moderator, decision="reject", note="Звука нет")

        self.assertEqual(wallet_of(self.author).balance, rewards.WELCOME)

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

    def test_the_long_pickers_can_be_searched(self):
        """Предметов под восемьдесят, преподавателей за сотню — без поиска до нужного
        приходится листать колесом. Семестрам он, наоборот, ни к чему: их дюжина
        и они по порядку, а лишнее поле ввода только отнимает первый тык.

        Флаг живёт на виджете (core.widgets), и потерять его при правке формы —
        дело одной строки: именно так поиск и пропал у предмета до 01.09.2026.
        """
        fields = PlaylistForm().fields

        self.assertTrue(fields["subject"].widget.search)
        self.assertTrue(fields["teachers"].widget.search)
        self.assertFalse(fields["terms"].widget.search)

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


class TelegramTests(LectoriumTests):
    """Модерации в чат — как о материале и книге: очередь на сайте есть, но в неё надо
    зайти, а курс лекций ждёт проверки неделями."""

    def setUp(self):
        self.keeper = make_user("k@t.local", surname="Хранов")
        self.keeper.user_permissions.add(Permission.objects.get(codename="add_playlist"))
        self.keeper = User.objects.get(pk=self.keeper.pk)
        self.client.force_login(self.keeper)

    def create(self):
        return self.client.post(reverse("playlist_new"), {
            "title": "Механика", "subject": self.subject.pk, "year": 2026, "synopsis": "",
        })

    def test_a_new_course_reaches_the_chat(self):
        with mock.patch("lectorium.views.notify") as notify:
            self.create()

        chat, template, context = notify.call_args.args
        self.assertEqual(chat, "moderation")
        self.assertEqual(template, "telegram/playlist_pending.html")
        self.assertTrue(context["created"])
        self.assertEqual(context["playlist"], Playlist.objects.get())

    def test_an_edited_course_says_it_came_back(self):
        playlist = self.make_playlist(status=Playlist.Status.APPROVED, lectures=0)
        Playlist.objects.filter(pk=playlist.pk).update(uploader=self.keeper)

        with mock.patch("lectorium.views.notify") as notify:
            self.client.post(reverse("playlist_edit", args=[playlist.pk]), {
                "title": "Другое имя", "subject": self.subject.pk, "year": 2026, "synopsis": "",
            })

        self.assertFalse(notify.call_args.args[2]["created"])

    def test_a_moderator_publishing_his_own_tells_nobody(self):
        """Он и есть тот, кому сообщали бы."""
        self.client.force_login(self.moderator)
        self.moderator.user_permissions.add(Permission.objects.get(codename="add_playlist"))

        with mock.patch("lectorium.views.notify") as notify:
            self.create()

        notify.assert_not_called()

    def test_a_deleted_course_reaches_the_chat_with_what_was_lost(self):
        playlist = self.make_playlist(lectures=2)
        self.client.force_login(self.author)

        with mock.patch("lectorium.views.notify") as notify, self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("playlist_delete", args=[playlist.pk]))

        chat, template, context = notify.call_args.args
        self.assertEqual((chat, template), ("moderation", "telegram/playlist_deleted.html"))
        self.assertEqual(context["lectures"], 2)

    def test_the_message_carries_what_the_moderator_needs(self):
        playlist = self.make_playlist(lectures=2)
        playlist.terms.add(Term.objects.create(number=3))
        playlist.teachers.add(Teacher.objects.create(name="Пётр", surname="Сорокоумов"))

        text = render_to_string("telegram/playlist_pending.html", {
            "playlist": playlist, "editor": self.author, "created": True,
            "url": "https://knt-mipt.ru/lectures/1/",
        })

        self.assertIn("Новый курс лекций", text)
        self.assertIn(playlist.title, text)
        self.assertIn("https://knt-mipt.ru/lectures/1/", text)
        self.assertIn("Физика", text)
        self.assertIn("Сорокоумов", text)
        self.assertIn("<b>Записей:</b> 2", text)
        self.assertIn("Иванов Иван", text)


class RecordEditTests(LectoriumTests):
    """Записи правятся на форме курса: поле у них ровно одно, своей страницы не надо."""

    def setUp(self):
        self.playlist = self.make_playlist(lectures=2)
        self.first, self.second = self.playlist.lectures.all()
        self.client.force_login(self.author)

    def save(self, **extra):
        body = {"title": self.playlist.title, "subject": self.subject.pk, "year": 2026, "synopsis": ""}
        return self.client.post(reverse("playlist_edit", args=[self.playlist.pk]), {**body, **extra})

    def left(self, prefix):
        try:
            folders, files = file_storage().listdir(prefix)
        except (FileNotFoundError, NotADirectoryError):
            return 0
        return len(files) + len(folders)

    def test_the_form_lists_the_records(self):
        page = self.client.get(reverse("playlist_edit", args=[self.playlist.pk])).content.decode()

        self.assertIn(f'name="name-{self.first.pk}"', page)
        self.assertIn(self.second.title, page)

    def test_a_record_is_renamed(self):
        self.save(**{f"name-{self.first.pk}": "Первая пара", f"name-{self.second.pk}": self.second.title})

        self.first.refresh_from_db()
        self.assertEqual(self.first.title, "Первая пара")

    def test_a_marked_record_goes_and_takes_its_video(self):
        make_set(self.first.prefix)
        with self.captureOnCommitCallbacks(execute=True):
            self.save(**{f"delete-{self.first.pk}": "on", f"name-{self.second.pk}": self.second.title})

        self.assertEqual([one.pk for one in self.playlist.lectures.all()], [self.second.pk])
        self.assertFalse(self.left(self.first.prefix))

    def test_dragging_a_record_changes_its_place(self):
        """Порядок присылается скрытыми input name="order": перетаскивание переставляет
        сами строки, а браузер отправляет поля в порядке разметки."""
        self.save(**{
            "order": [self.second.pk, self.first.pk],
            f"name-{self.first.pk}": self.first.title,
            f"name-{self.second.pk}": self.second.title,
        })

        self.assertEqual(
            [one.pk for one in self.playlist.lectures.all()], [self.second.pk, self.first.pk],
        )

    def test_a_record_deleted_in_the_same_go_does_not_break_the_order(self):
        """Удалённая запись в присланном списке ещё встречается — её строку убирает
        браузер только после перезагрузки."""
        third = Lecture.objects.create(playlist=self.playlist, title="Третья", order=2, prefix="x")

        self.save(**{
            "order": [third.pk, self.first.pk, self.second.pk],
            f"delete-{self.first.pk}": "on",
            f"name-{self.second.pk}": self.second.title,
        })

        self.assertEqual([one.pk for one in self.playlist.lectures.all()], [third.pk, self.second.pk])
        self.assertEqual([one.order for one in self.playlist.lectures.all()], [0, 2])

    def test_the_form_can_be_dragged_at_all(self):
        """Плагин перетаскивания грузится только там, где он нужен, — забыть его
        значит получить страницу, где ручка есть, а тянуть нельзя."""
        page = self.client.get(reverse("playlist_edit", args=[self.playlist.pk])).content.decode()

        self.assertIn("alpine-sort", page)
        self.assertIn("x-sort:item", page)

    def test_an_unmarked_record_stays(self):
        """Промах по кнопке не должен стоить двухчасовой лекции: пока не нажато
        «Сохранить», ничего не происходит."""
        self.client.get(reverse("playlist_edit", args=[self.playlist.pk]))

        self.assertEqual(self.playlist.lectures.count(), 2)

    def test_deleting_the_course_takes_every_video_with_it(self):
        prefixes = [one.prefix for one in self.playlist.lectures.all()]
        for prefix in prefixes:
            make_set(prefix)

        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(reverse("playlist_delete", args=[self.playlist.pk]))

        self.assertRedirects(answer, reverse("playlist_list"))
        self.assertEqual(Playlist.objects.count(), 0)
        self.assertEqual([self.left(prefix) for prefix in prefixes], [0, 0])

    def test_a_stranger_deletes_nothing(self):
        self.client.force_login(self.stranger)

        # Чужой непроверенный курс не виден вовсе, а одобренный виден, но не его.
        self.assertEqual(self.client.post(reverse("playlist_delete", args=[self.playlist.pk])).status_code, 403)
        self.assertEqual(Playlist.objects.count(), 1)

    def test_moderation_deletes_anything(self):
        self.client.force_login(self.moderator)

        self.client.post(reverse("playlist_delete", args=[self.playlist.pk]))

        self.assertEqual(Playlist.objects.count(), 0)


class StageTests(LectoriumTests):
    """Что написано вместо кадра, пока набора нет.

    «Обрабатывается» у записи, до которой пекарня ещё не дошла, — неправда: очередь
    стоит, пока не включат машину с видеокартой, и человек всё это время ждёт готового
    с минуты на минуту.
    """

    def setUp(self):
        self.playlist = self.make_playlist(lectures=1)
        self.lecture = self.playlist.lectures.get()
        Lecture.objects.filter(pk=self.lecture.pk).update(prefix="")
        self.lecture.refresh_from_db()
        self.job = MediaJob.objects.create(recipe="lecture", source="uploads/a/z.mkv", lecture=self.lecture)

    def stage(self, status):
        MediaJob.objects.filter(pk=self.job.pk).update(status=status)
        return Lecture.objects.get(pk=self.lecture.pk).stage()

    def test_a_record_nobody_took_yet_is_in_the_queue(self):
        self.assertEqual(self.stage(MediaJob.Status.WAITING), "в очереди")

    def test_a_record_in_the_oven_says_so(self):
        self.assertEqual(self.stage(MediaJob.Status.BAKING), "обрабатывается")

    def test_a_record_that_fell_over_says_so(self):
        self.assertEqual(self.stage(MediaJob.Status.FAILED), "не обработалась")

    def test_a_record_without_a_job_is_not_waiting_in_any_queue(self):
        """Такую завели руками в админке, чтобы прицепить набор, испечённый отдельно."""
        self.job.delete()

        self.assertEqual(Lecture.objects.get(pk=self.lecture.pk).stage(), "набор не привязан")

    def test_the_page_says_it_out_loud(self):
        self.client.force_login(self.author)

        page = self.client.get(reverse("playlist_detail", args=[self.playlist.pk])).content.decode()

        self.assertIn("в очереди", page)
        self.assertNotIn("обрабатывается", page)


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
