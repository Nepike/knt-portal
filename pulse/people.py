"""«Люди» в Пульсе: поиск, подборки, карточка человека и действия над ним.

Действия идут через те же сервисы, что и сайт (economy.services, wall.services), с тем же
правом, что в админке, и пишутся в историю админки на самого человека — так в его
истории видно всё, что с ним делали, откуда бы ни делали.
"""

from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.models import Session
from django.db.models import Count, Max, Q
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from chats.models import Membership, Message
from comments.models import Comment
from cosmetics.models import CosmeticItem, UserItem
from cosmetics.services import grant as give
from core.search import by_name
from economy.models import BalanceLog
from economy.services import NotEnoughFunds, credit, spend
from lectorium.models import Playlist
from library.models import Book
from materials.models import Material
from printing import services as printing
from printing.models import PrintAccount
from users import impersonation
from users.models import User
from users.sessions import alive
from wall import services as wall
from wall.models import WallProfile

from . import actions, feed
from .access import GROUP, logged

PAGE = 50
PRESETS = {
    "never": "Ни разу не входили",
    "rights": "С особыми правами",
    "banned": "С запретами",
    "off": "Выключенные",
}
# (группа, как назвать, что даёт). В карточке — только те, что есть в базе: «Модераторов»
# на проде завели руками, миграции о них не знают.
RIGHTS = [
    ("Модераторы", "Модератор", "проверяет материалы, книги, отзывы"),
    ("Кураторы", "Куратор чата курса", "ведёт чаты курсов"),
    ("Доступ при закрытии", "Проход при закрытии", "видит сайт, когда он закрыт"),
    ("Печать без лимита", "Печать без лимита", "недельный лимит не действует"),
    ("Правка Стены", "Правка Стены", "стирает, защищает области, закрывает доску"),
    (GROUP, "Пульс", "видит этот раздел"),
]
WALL_BANS = {0: "открыта", 1: "сутки", 7: "неделя", 30: "месяц"}
# Сколько живёт сессия для проверки. Коротко: ключ — пароль на предъявителя.
TEST_SESSION = {30: "30 минут", 120: "2 часа"}
RECENT = 10


class Refused(Exception):
    """Действие не выполнено — причина уходит человеку сообщением."""


def find(query="", preset=""):
    # Последний раз — что свежее: вход или запрос живой сессии.
    last = Max("sessions__seen")
    people = User.objects.select_related("team", "wallet").annotate(
        seen=Greatest(Coalesce(last, "last_login"), Coalesce("last_login", last)),
    )
    if preset == "never":
        people = people.filter(is_active=True, last_login__isnull=True, sessions__isnull=True)
    elif preset == "rights":
        people = people.filter(Q(is_superuser=True) | Q(groups__name__in=[name for name, *_ in RIGHTS])).distinct()
    elif preset == "banned":
        people = people.filter(Q(print_account__banned=True) | Q(wall__banned_until__gt=timezone.now())).distinct()
    elif preset == "off":
        people = people.filter(is_active=False)
    if query:
        people = by_name(people, query, fields=("surname", "name", "email"))
    else:
        people = people.order_by("surname", "name")
    return people


def rights(person):
    """Права переключателями: (группа, подпись, что даёт, включено). Своё право бывает
    и выданным напрямую, мимо группы, — такое тоже считается включённым."""
    existing = {group.name: group for group in Group.objects.filter(name__in=[name for name, *_ in RIGHTS])}
    held = set(person.groups.values_list("name", flat=True))
    direct = set(person.user_permissions.values_list("pk", flat=True))
    rows = []
    for name, label, note in RIGHTS:
        group = existing.get(name)
        if group is None:
            continue
        on = name in held or bool(direct & set(group.permissions.values_list("pk", flat=True)))
        rows.append({"group": name, "label": label, "note": note, "on": on})
    return rows


def card(person, viewer):
    """Всё для карточки. Текст переписки не показываем никогда — только числа."""
    account = PrintAccount.objects.filter(user=person).first()
    profile = WallProfile.objects.filter(user=person).first()
    works = []
    for title, model in (("Материалы", Material), ("Книги", Book), ("Курсы лекций", Playlist)):
        mine = model.objects.filter(uploader=person).order_by("-created")
        works.append({"title": title, "count": mine.count(), "items": list(mine[:RECENT])})
    texts = actions.offered(
        feed.events(timezone.now(), kinds=["review", "comment"], people=[person.pk], limit=RECENT), viewer,
    )
    wallet = getattr(person, "wallet", None)
    # Что этому смотрящему можно нажать: то же право, что в админке, а суперпользователя
    # меняет только суперпользователь.
    locked = person.is_superuser and not viewer.is_superuser
    can = {
        "print": viewer.has_perm("printing.change_printaccount") and not locked,
        "user": viewer.has_perm("users.change_user") and not locked,
        "wall": viewer.has_perm(wall.MODERATOR),
        "tokens": viewer.has_perm("economy.add_balancelog"),
        "gift": viewer.has_perm("cosmetics.add_useritem"),
    }
    return {
        "sessions": list(alive(person)),
        "logins": list(person.login_events.all()[:20]),
        "works": works,
        "texts": texts,
        "reviews": person.teacher_reviews.count(),
        "comments": Comment.objects.filter(author=person).count(),
        "balance": wallet.balance if wallet else 0,
        "entries": list(BalanceLog.objects.filter(wallet__user=person).select_related("by")[:8]),
        "print": printing.limits(person),
        "print_banned": bool(account and account.banned),
        "painted": profile.painted if profile else 0,
        "wall_until": profile.banned_until if profile and profile.banned_until and profile.banned_until > timezone.now() else None,
        # «said», а не «messages»: это имя в шаблоне занято сообщениями сайта (тостами).
        "said": Message.objects.filter(author=person, deleted=False).aggregate(n=Count("id"), last=Max("created")),
        "chats": Membership.objects.filter(user=person).count(),
        "rights": [
            {**row, "can": can["user"] and not (row["group"] == GROUP and row["on"] and person == viewer)}
            for row in rights(person)
        ],
        "wall_bans": WALL_BANS,
        "test_session": TEST_SESSION,
        "owned": UserItem.objects.filter(user=person).count(),
        # Подарить можно то, чего у человека ещё нет: выдача уже имеющегося ничего не меняет.
        "giftable": list(
            CosmeticItem.objects.exclude(owners__user=person).order_by("kind", "rarity", "name")
        ) if can["gift"] else [],
        "can": can,
    }


def _guard(viewer, person, perm):
    if not viewer.has_perm(perm):
        raise Refused("Для этого нужно право, которого у тебя нет.")
    if person.is_superuser and not viewer.is_superuser:
        raise Refused("Суперпользователя меняет только суперпользователь.")


def ban_printing(request, person, banned):
    _guard(request.user, person, "printing.change_printaccount")
    PrintAccount.objects.update_or_create(user=person, defaults={"banned": banned})
    logged(request, person, "печать закрыта" if banned else "печать открыта")
    return "Печать закрыта" if banned else "Печать открыта"


def ban_wall(request, person, days):
    if days not in WALL_BANS:
        raise Refused("Такого срока нет.")
    try:
        wall.ban(request.user, person, days)
    except wall.WallError as error:
        raise Refused(str(error).capitalize()) from error
    text = f"Стена закрыта: {WALL_BANS[days]}" if days else "Стена открыта"
    logged(request, person, text.lower())
    return text


def _end_sessions(person, pk=None):
    doomed = alive(person)
    if pk is not None:
        doomed = doomed.filter(pk=pk)
    return Session.objects.filter(pk__in=doomed.values("session_id")).delete()[0]


def set_active(request, person, active):
    _guard(request.user, person, "users.change_user")
    if person == request.user and not active:
        raise Refused("Свой аккаунт отсюда не выключить.")
    person.is_active = active
    person.save(update_fields=["is_active"])
    if not active:
        _end_sessions(person)
    logged(request, person, "аккаунт включён" if active else "аккаунт выключен, сессии закрыты")
    return "Аккаунт включён" if active else "Аккаунт выключен, сессии закрыты"


def set_right(request, person, name, on):
    _guard(request.user, person, "users.change_user")
    label = next((label for group, label, _ in RIGHTS if group == name), None)
    group = Group.objects.filter(name=name).first()
    if label is None or group is None:
        raise Refused("Такого права нет.")
    if name == GROUP and person == request.user and not on:
        raise Refused("Своё право на Пульс отсюда не снять — иначе обратно не попасть.")
    if on:
        person.groups.add(group)
    else:
        person.groups.remove(group)
        # Выданное напрямую тоже снимаем: иначе переключатель остался бы включённым.
        person.user_permissions.remove(*Permission.objects.filter(group=group))
    logged(request, person, f"право «{label}» {'выдано' if on else 'снято'}")
    return f"«{label}»: {'выдано' if on else 'снято'}"


def end_sessions(request, person, pk=None):
    _guard(request.user, person, "users.change_user")
    closed = _end_sessions(person, pk)
    logged(request, person, f"закрыто сессий: {closed}")
    return f"Закрыто сессий: {closed}"


def grant(request, person, amount, note):
    """Токены вручную. Списание в минус не уводит — как в админке."""
    if not request.user.has_perm("economy.add_balancelog"):
        raise Refused("Для этого нужно право, которого у тебя нет.")
    if not amount:
        raise Refused("Нулевая операция ничего не меняет.")
    if not note:
        raise Refused("Напиши, за что: человек увидит это в своём кошельке.")
    move = credit if amount > 0 else spend
    try:
        move(person, abs(amount), BalanceLog.Reason.MANUAL, note=note[:200], by=request.user)
    except NotEnoughFunds as error:
        raise Refused("Столько списать нельзя: баланс ушёл бы в минус.") from error
    logged(request, person, f"токены вручную: {amount:+} «{note[:200]}»")
    return f"Токены: {amount:+}"


def password(request, person, how):
    _guard(request.user, person, "users.change_user")
    if how == "change":
        person.must_change_password = True
        person.save(update_fields=["must_change_password"])
        logged(request, person, "сменить пароль при следующем входе")
        return "Попросим сменить пароль при следующем входе"
    if how == "link":
        form = PasswordResetForm({"email": person.email})
        form.is_valid()
        form.save(
            request=request, use_https=request.is_secure(),
            email_template_name="users/password_reset_email.txt",
            subject_template_name="users/password_reset_subject.txt",
        )
        logged(request, person, "выслана ссылка для сброса пароля")
        return f"Ссылка для сброса ушла на {person.email}"
    raise Refused("Такого действия нет.")


def test_session(request, person, minutes):
    """Ключ сессии этого человека — посмотреть сайт его глазами в приватном окне
    (users/impersonation.py). Только суперпользователю: это вход в чужой аккаунт целиком."""
    if not request.user.is_superuser:
        raise Refused("Сессию для проверки выдаёт только суперпользователь.")
    if minutes not in TEST_SESSION:
        raise Refused("Такого срока нет.")
    if not person.is_active:
        raise Refused("Аккаунт выключен — сессия всё равно не пустит.")
    if person == request.user:
        raise Refused("Своя сессия у тебя и так есть.")
    key = impersonation.mint(person, minutes)
    logged(request, person, f"выдана сессия для проверки на {TEST_SESSION[minutes]}")
    return key


def gift(request, person, item):
    """Подарить вещь из магазина или кейса — в инвентарь, без списания токенов."""
    if not request.user.has_perm("cosmetics.add_useritem"):
        raise Refused("Для этого нужно право, которого у тебя нет.")
    if item is None:
        raise Refused("Такой вещи нет.")
    if UserItem.objects.filter(user=person, item=item).exists():
        raise Refused(f"«{item.name}» уже есть в инвентаре.")
    give(person, item)
    logged(request, person, f"подарена вещь «{item.name}»")
    return f"Подарено: «{item.name}»"
