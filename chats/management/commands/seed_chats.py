"""Демо-переписка, чтобы посмотреть чаты вживую. ТОЛЬКО для разработки.

Собеседников берём из базы — реальных студентов; выдуманы только сообщения. Часть
диалогов оставляем непрочитанной, часть с реакциями и ответом на сообщение: иначе
не увидеть ни черты «непрочитанные», ни пачек, ни строки реакций.

Состав собеседников определяется id того, кому заводим (random.seed), поэтому повтор
даёт ту же демку, а `--wipe` находит ровно её и ничего чужого.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from chats.models import REACTIONS, Chat, Membership, Message
from users.models import User

# Диалоги: (дней назад, сколько сообщений в конце оставить непрочитанными, реплики).
# В реплике 0 — говорит собеседник, 1 — тот, кому заводим демку.
TALKS = [
    (0, 2, [
        (0, "привет! ты идёшь завтра на семинар к Петрову?"),
        (1, "да, собирался"),
        (0, "можешь занять место? я с первой пары опаздываю"),
        (1, "ок, займу"),
        (0, "спасибо 🙏"),
        (0, "а, и ещё: скинь пожалуйста разбор листка 7, у меня страницы не открываются"),
    ]),
    (1, 0, [
        (1, "слушай, а ты сдал лабу по механике?"),
        (0, "сдал, но с третьего раза"),
        (0, "там главное таблицу погрешностей нормально оформить, остальное не смотрят"),
        (1, "понял, спасибо"),
        (1, "а шаблон отчёта есть?"),
        (0, "https://knt-mipt.ru/materials/ вот тут, называется «Лабораторные работы по общей физике»"),
        (1, "нашёл, спасибо большое"),
    ]),
    (3, 1, [
        (0, "ты не знаешь, когда пересдача по алгебре?"),
        (1, "вроде на следующей неделе, но точно не скажу"),
        (0, "ладно, спрошу у старосты"),
        (0, "узнал: 14-го в 15:00, ауд. 202"),
    ]),
    (6, 0, [
        (1, "привет, а конспект по теорверу у тебя остался?"),
        (0, "остался, но он от руки и местами нечитаемый честно говоря"),
        (1, "сойдёт, у меня вообще ничего нет"),
        (0, "тогда завтра принесу"),
    ]),
    (12, 4, [
        (0, "в чате курса писали, что расписание поменяли"),
        (1, "опять?"),
        (0, "да, вторая пара теперь в другом корпусе"),
        (0, "и семинар перенесли на пятницу"),
        (0, "проверь у себя, у меня в приложении не обновилось"),
        (0, "всё, обновилось"),
    ]),
]

# Группы: (название, участников кроме меня, дней назад, непрочитанных, реплики)
GROUPS = [
    ("Проект по программированию", 3, 2, 3, [
        "Ребят, давайте определимся с темой до пятницы",
        "я предлагаю парсер расписания",
        "звучит норм, только надо понять, откуда брать данные",
        "с сайта института, там таблица",
        "таблица там кривая, я пробовал. Проще руками занести один раз",
        "давайте попробуем распарсить, если не выйдет — занесём",
        "ок, я возьму на себя парсинг",
        "а я тогда интерфейс",
        "договорились. Встречаемся в среду после третьей пары?",
        "я не смогу в среду, давайте в четверг",
        "хорошо, четверг",
    ]),
    ("Общага, 5 этаж", 5, 9, 0, [
        "кто-нибудь оставлял кастрюлю на кухне?",
        "моя, извините, сейчас заберу",
        "спасибо)",
        "народ, у кого есть переходник на type-c?",
        "у меня есть, заходи в 512",
        "выручил, спасибо",
        "напоминаю про уборку в субботу",
        "буду",
        "я тоже",
    ]),
]

TITLES = [title for title, *_ in GROUPS]


class Command(BaseCommand):
    help = "Демо-переписка для разработки (диалоги и группы)"

    def add_arguments(self, parser):
        parser.add_argument("--user", default="1", help="кому заводим: id или почта (по умолчанию 1)")
        parser.add_argument("--wipe", action="store_true", help="снести заведённое этой командой")

    def handle(self, *args, **options):
        me = self._find(options["user"])
        cast = self._cast(me)
        if options["wipe"]:
            return self._wipe(me, cast)

        with transaction.atomic():
            for index, (days_ago, unread, lines) in enumerate(TALKS):
                self._talk(me, cast[index], days_ago, unread, lines)
            for index, (title, size, days_ago, unread, lines) in enumerate(GROUPS):
                crowd = cast[len(TALKS) + index * size:][:size]
                self._group(me, crowd, title, days_ago, unread, lines)

        self.stdout.write(self.style.SUCCESS(
            f"диалогов: {len(TALKS)}, групп: {len(GROUPS)} у {me.full_name} "
            f"(убрать: manage.py seed_chats --user {options['user']} --wipe)"
        ))

    def _find(self, value):
        who = User.objects.filter(pk=value).first() if value.isdigit() else None
        who = who or User.objects.filter(email__iexact=value).first()
        if not who:
            raise CommandError(f"не нашёл пользователя «{value}»")
        return who

    def _cast(self, me):
        """Собеседники демки. Всегда те же самые: от этого зависит, найдёт ли `--wipe`
        именно свои диалоги, а не все личные переписки человека."""
        pool = list(User.objects.filter(is_active=True).exclude(pk=me.pk).order_by("pk"))
        need = len(TALKS) + sum(size for _, size, *_ in GROUPS)
        if len(pool) < need:
            raise CommandError(f"в базе слишком мало людей: нужно {need}, есть {len(pool)}")
        return random.Random(me.pk).sample(pool, need)

    def _wipe(self, me, cast):
        keys = [Chat.dm_key_for(me, one) for one in cast[: len(TALKS)]]
        dms = Chat.objects.filter(kind="dm", dm_key__in=keys)
        groups = Chat.objects.filter(kind="group", title__in=TITLES, memberships__user=me)
        count = dms.count() + groups.distinct().count()
        dms.delete()
        groups.distinct().delete()
        self.stdout.write(self.style.SUCCESS(f"снесено чатов: {count}"))

    def _say(self, chat, author, text, when, reply_to=None):
        message = Message.objects.create(chat=chat, author=author, text=text, reply_to=reply_to)
        # created заполняет default, своё значение он бы затёр
        Message.objects.filter(pk=message.pk).update(created=when)
        message.refresh_from_db()
        return message

    def _finish(self, chat, me, messages, unread, others_read=1):
        """Последнее сообщение чата и курсоры чтения.

        Свой отодвинут на `unread` назад, чужие — вразнобой: без этого «кто прочитал»
        всегда отвечает пустым списком, и посмотреть на него не на чем.
        """
        Chat.objects.filter(pk=chat.pk).update(last_message=messages[-1])
        read_to = messages[-1 - unread] if 0 < unread < len(messages) else messages[-1]
        Membership.objects.filter(chat=chat, user=me).update(last_read=read_to)

        rest = list(Membership.objects.filter(chat=chat).exclude(user=me))
        for index, membership in enumerate(rest):
            # Первые `others_read` дочитали до конца, остальные отстали на несколько реплик
            behind = 0 if index < others_read else min(3 + index, len(messages) - 1)
            Membership.objects.filter(pk=membership.pk).update(last_read=messages[-1 - behind])

    def _talk(self, me, other, days_ago, unread, lines):
        chat = Chat.get_or_create_dm(me, other)
        chat.messages.all().delete()  # повторный запуск не должен дописывать вторую копию
        start = timezone.now() - timedelta(days=days_ago, minutes=len(lines) * 7)
        messages = []
        for step, (mine, text) in enumerate(lines):
            # Последняя реплика — ответом на предпоследнюю чужую: цитата тоже часть демки
            reply = messages[-2] if step == len(lines) - 1 and len(messages) > 2 else None
            messages.append(self._say(
                chat, me if mine else other, text, start + timedelta(minutes=step * 7), reply
            ))
        messages[2].reactions.get_or_create(user=other, emoji=REACTIONS[0])
        # В части диалогов собеседник дочитал не до конца: видны обе галочки
        self._finish(chat, me, messages, unread, others_read=days_ago % 2)

    def _group(self, me, crowd, title, days_ago, unread, lines):
        chat, _ = Chat.objects.get_or_create(kind="group", title=title)
        chat.messages.all().delete()
        Membership.objects.bulk_create(
            [Membership(chat=chat, user=me)]
            + [Membership(chat=chat, user=one, is_admin=index == 0) for index, one in enumerate(crowd)],
            ignore_conflicts=True,
        )
        start = timezone.now() - timedelta(days=days_ago, minutes=(len(lines) + 1) * 11)
        speakers = [crowd[0], me, *crowd[1:]]
        messages = [self._say(chat, None, f"{crowd[0].name} {crowd[0].surname} создал(а) группу", start)]
        for step, text in enumerate(lines):
            messages.append(self._say(
                chat, speakers[step % len(speakers)], text, start + timedelta(minutes=(step + 1) * 11)
            ))
        for emoji, one in zip(REACTIONS, crowd):
            messages[3].reactions.get_or_create(user=one, emoji=emoji)
        self._finish(chat, me, messages, unread)
