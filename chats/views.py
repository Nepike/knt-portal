import json
import re
from datetime import timedelta

from django.contrib import messages as django_messages
from django.db.models import F, Prefetch
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.search import by_name
from core.throttle import throttled
from users.models import User, UserSession

from .events import notify_chat, notify_joined, notify_left, notify_read
from .forms import AddMembersForm, CuratorAddForm, GroupChatForm
from .mentions import remember
from .models import REACTIONS, Chat, Membership, Message, unread_total
from .uploads import attach, limits, problems

MESSAGE_RELATIONS = ("author", "reply_to", "reply_to__author")
# Вложения цитаты — ради её подписи у сообщения без текста (фильтр preview).
MESSAGE_SETS = ("reactions", "images", "files", "reply_to__images", "reply_to__files", "mentions")
PAGE_SIZE = 30  # сообщений в порции истории
CATCH_UP = 100  # столько догоняем порцией, дальше проще перерисовать страницу
MAX_TEXT = 4000  # он же maxlength поля ввода, шаблонам уезжает как max_text
SEND_LIMIT = 30  # сообщений в минуту с одного человека
# Правки, реакции и удаления тоже расходятся событием — предел и им, повыше: реакции ставят подряд.
ACT_LIMIT = 60
# Поиск: короче двух букв искать нечего, хватает полусотни находок.
SEARCH_MIN = 2
SEARCH_LIMIT = 50
# Через столько молчания сообщение того же автора начинает новую пачку.
PACK_GAP = timedelta(minutes=10)
# «В сети»: отметка активности свежеет раз в пять минут (users/sessions.py) — окно вдвое шире.
ONLINE_WINDOW = timedelta(minutes=10)
# Три и больше переводов строки подряд (пробелы в пустых строках не в счёт).
BLANK_LINES = re.compile(r"(?:\n[^\S\n]*){3,}")


def _clean(text):
    """Текст сообщения: обрезать края и длину, сжать пустые строки подряд."""
    return BLANK_LINES.sub("\n\n", text.strip())[:MAX_TEXT]


def _int(value):
    """id из GET/POST: мусор превращаем в 0, а не в 500."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _chat_items(user):
    items = list(
        Membership.objects.filter(user=user)
        .exclude(chat__kind="dm", chat__last_message__isnull=True)  # пустые ЛС не показываем
        .with_unread(user)
        .select_related("chat", "chat__last_message", "chat__last_message__author")
        # Участники — только у ЛС (нужен собеседник): в чате курса их сотни.
        .prefetch_related(
            Prefetch("chat__memberships", queryset=Membership.objects.filter(chat__kind="dm").select_related("user")),
            # Вложения последнего сообщения — ради его подписи в списке (фильтр preview)
            "chat__last_message__images",
            "chat__last_message__files",
        )
        # Закреплённые сверху, дальше — по времени последнего сообщения: между чатами id
        # не порядок (импорт и демка заводили переписку задним числом).
        .order_by("-pinned", F("chat__last_message__created").desc(nulls_last=True))
    )
    for item in items:
        item.other = item.chat.other_member(user)  # None для групп
    return items


def _membership(request, pk):
    """Он же проверка доступа: не участник — 404. Один запрос: состав чата фрагментам не нужен."""
    return get_object_or_404(Membership.objects.select_related("chat"), chat_id=pk, user=request.user)


def _membership_page(request, pk):
    """То же + состав чата: нужен только целой странице (шапка, модалка участников)."""
    return get_object_or_404(
        Membership.objects.select_related("chat").prefetch_related(
            Prefetch("chat__memberships", queryset=Membership.objects.select_related("user__team")),
        ),
        chat_id=pk,
        user=request.user,
    )


def _mark_read(membership, messages):
    if messages and (membership.last_read_id or 0) < messages[-1].pk:
        membership.last_read = messages[-1]
        membership.save(update_fields=["last_read"])
        if membership.chat.kind == "dm":
            notify_read(membership.chat_id, membership.user_id, messages[-1].pk)


def _read_upto(chat, user):
    """Докуда дочитал собеседник. Только для ЛС — на этом держатся галочки в пузыре."""
    if chat.kind != "dm":
        return 0
    cursor = chat.memberships.exclude(user=user).values_list("last_read_id", flat=True).first()
    return cursor or 0


def _message(request, pk):
    """Он же проверка доступа: не участник чата — 404."""
    return get_object_or_404(
        Message.objects.select_related("chat", *MESSAGE_RELATIONS).prefetch_related(*MESSAGE_SETS),
        pk=pk,
        chat__memberships__user=request.user,
    )


def _feed(chat):
    """Лента чата со всем, что нужно пузырю: автор, цитата, реакции, вложения."""
    return chat.messages.select_related(*MESSAGE_RELATIONS).prefetch_related(*MESSAGE_SETS)


def _history_page(chat, before=0):
    """Порция истории: PAGE_SIZE сообщений старше `before` (0 — самые свежие)."""
    qs = _feed(chat)
    if before:
        qs = qs.filter(id__lt=before)
    # +1 — сразу и признак «есть ещё», и сосед порции сверху, по которому видно, тот же день
    page = list(qs.order_by("-id")[: PAGE_SIZE + 1])
    older = page[PAGE_SIZE] if len(page) > PAGE_SIZE else None
    return _mark_breaks(list(reversed(page[:PAGE_SIZE])), older), older is not None


def _first_unread(membership):
    """Id первого непрочитанного ЧУЖОГО сообщения или 0."""
    return (
        membership.chat.messages
        .filter(id__gt=membership.last_read_id or 0, deleted=False)
        .exclude(author_id=membership.user_id)
        .values_list("id", flat=True)
        .first()
    ) or 0


def _opening_page(membership):
    """С чего открыть чат: (сообщения, есть ли выше, id первого непрочитанного). С первого
    непрочитанного, если оно есть и его не больше CATCH_UP; иначе — конец переписки."""
    unread_from = _first_unread(membership)
    if unread_from:
        page = list(_feed(membership.chat).filter(id__gte=unread_from)[: CATCH_UP + 1])
        if len(page) <= CATCH_UP:
            older = _neighbour(membership.chat, unread_from)
            return _mark_breaks(page, older), older is not None, unread_from
    messages, has_more = _history_page(membership.chat)
    # Отметку ставим, только если сообщение и правда попало на экран
    return messages, has_more, unread_from if any(m.pk == unread_from for m in messages) else 0


def _around(chat, at):
    """Окно ленты вокруг сообщения, по PAGE_SIZE в обе стороны — для находки поиска.
    Пусто — такого нет."""
    older = list(_feed(chat).filter(id__lt=at).order_by("-id")[:PAGE_SIZE])
    rest = list(_feed(chat).filter(id__gte=at)[: PAGE_SIZE + 1])
    if not rest or rest[0].pk != at:
        return [], False, False
    # +1 сверх окна — признак, что ниже есть ещё; сам он в ленту не идёт
    newer, more_below = rest[:PAGE_SIZE], len(rest) > PAGE_SIZE
    page = list(reversed(older)) + newer
    return _mark_breaks(page, _neighbour(chat, page[0].pk)), len(older) == PAGE_SIZE, more_below


def _catch_up(chat, after):
    """Пропущенное с курсора и признак «слишком много»: больше CATCH_UP не сшиваем —
    страница перерисуется, у неё своя пагинация."""
    found = list(_feed(chat).filter(id__gt=after)[: CATCH_UP + 1])
    return found[:CATCH_UP], len(found) > CATCH_UP


def _refresh():
    """Ответ htmx «перечитай страницу целиком»."""
    return HttpResponse(headers={"HX-Refresh": "true"})


def _neighbour(chat, before_id):
    """Сообщение прямо перед `before_id` — для дня и пачки. Без связей и не через
    `chat.messages`: менеджер связи читал бы `chat_id` вторым запросом."""
    if not before_id:
        return None
    return Message.objects.filter(chat=chat, id__lt=before_id).only("created", "author").last()


def _mark_breaks(messages, previous=None):
    """Отметить разрывы ленты: новый день (`day_break`) и новая пачка (`pack_start`) — подряд
    идущие сообщения автора без молчания дольше PACK_GAP. `previous` — сосед порции со стороны
    уже показанного: без него каждая порция начинала бы день и пачку заново.
    """
    for message in messages:
        message.day_break = previous is None or (
            timezone.localdate(message.created) != timezone.localdate(previous.created)
        )
        message.pack_start = (
            message.day_break
            or previous.author_id != message.author_id
            or message.created - previous.created > PACK_GAP
        )
        previous = message
    return messages


def _mark_one(chat, message):
    """Пометки для одиночного пузыря — он приезжает без соседей, а нарисовать себя должен
    так же, как в ленте: с аватаром, если начинает пачку."""
    _mark_breaks([message], _neighbour(chat, message.pk))
    # Дату рисует соседний узел, и подмену пузыря он переживёт: здесь она задвоилась бы.
    message.day_break = False
    return message


def _bubble(request, message):
    is_chat_admin = message.chat.memberships.filter(user=request.user, is_admin=True).exists()
    return render(request, "chats/_message.html", {
        "m": _mark_one(message.chat, message), "chat": message.chat, "is_chat_admin": is_chat_admin,
        "read_upto": _read_upto(message.chat, request.user),
    })


def _touch(message, kind):
    """Пометить контент изменённым: messages_new отдаст пузырь oob-заменой."""
    Message.objects.filter(pk=message.pk).update(updated=timezone.now())
    notify_chat(message.chat_id, kind=kind)


def _too_fast(request):
    """Действие над чужой лентой: предел один на правки, реакции и удаления."""
    return throttled(f"chat:act:{request.user.pk}", ACT_LIMIT, 60)


def _online_ids(chat):
    """Кто из участников заходил только что. Одним запросом на всю шапку: и число для
    группы, и «в сети» у собеседника в ЛС берутся отсюда же."""
    return set(
        UserSession.objects.filter(
            user__chat_memberships__chat=chat, seen__gte=timezone.now() - ONLINE_WINDOW
        ).values_list("user_id", flat=True)
    )


def _found_users(user, q=""):
    people = User.objects.filter(is_active=True).exclude(pk=user.pk).select_related("team")
    if q:
        return by_name(people, q)[:10]
    return people.order_by("surname", "name")[:10]


def _page_context(request, **extra):
    return {
        "items": _chat_items(request.user),
        "found_users": _found_users(request.user),
        "group_form": extra.pop("group_form", None) or GroupChatForm(creator=request.user),
        "max_text": MAX_TEXT,  # предел один на поле ввода, счётчик остатка и обрезку на сервере
        "upload_limits": json.dumps(limits()),
        **extra,
    }


def chat_list(request):
    # Сюда уводит вкладку, чей чат исчез. Редирект на себя: сообщение — в сессии, метка уходит из адреса.
    if "gone" in request.GET:
        django_messages.info(request, "Чат больше недоступен")
        return redirect("chat_list")
    return render(request, "chats/chat.html", _page_context(request))


def chat_detail(request, pk):
    membership = _membership_page(request, pk)
    # ?at= — открыться на сообщении из поиска: прочитанным не помечаем и за новыми не ходим
    # (курсор посреди истории).
    at = _int(request.GET.get("at"))
    messages, has_more, in_history = _around(membership.chat, at) if at else ([], False, False)
    unread_from = 0
    if not messages:
        at, in_history = 0, False
        messages, has_more, unread_from = _opening_page(membership)
        _mark_read(membership, messages)
    other = membership.chat.other_member(request.user)
    online = _online_ids(membership.chat)
    context = _page_context(
        request,
        chat=membership.chat,
        membership=membership,
        active_id=membership.chat_id,
        is_chat_admin=membership.is_admin,
        other=other,  # для шапки ЛС
        online=len(online),
        read_upto=_read_upto(membership.chat, request.user),
        other_online=other is not None and other.pk in online,
        # НЕ "messages" — имя занято django.contrib.messages (тосты в base.html)
        chat_messages=messages,
        has_more=has_more,
        unread_from=unread_from,  # перед ним лента рисует черту «непрочитанные»
        at=at,  # к нему лента прокрутится и подсветит
        in_history=in_history,  # лента стоит посреди истории: за новыми не ходим
    )
    if membership.chat.kind == "group" and membership.is_admin:
        context["add_form"] = AddMembersForm(chat=membership.chat)
    elif membership.chat.kind == "course":
        context["add_form"] = CuratorAddForm(chat=membership.chat)
        context["own_course"] = membership.chat.is_own_course(request.user)
    return render(request, "chats/chat.html", context)


def messages_new(request, pk):
    """Догрузка по сигналу сокета: новые (beforeend) + изменённые (oob-замена на месте)."""
    membership = _membership(request, pk)
    after = _int(request.GET.get("after"))
    messages, too_many = _catch_up(membership.chat, after)
    if too_many:
        return _refresh()
    # Правки, удаления и реакции последних секунд — окно с запасом, повтор oob безвреден.
    # Снизу — `since`, старейшее сообщение в ленте клиента: чужую замену htmx выбросит с ошибкой.
    # TODO: правку старше окна пропустившая вкладка не увидит — нужен второй курсор, по updated
    since = _int(request.GET.get("since"))
    window = timezone.now() - timedelta(seconds=12)
    updated = (
        list(_feed(membership.chat).filter(id__gte=since, id__lte=after, updated__gte=window))
        if after and since else []
    )
    _mark_read(membership, messages)
    if messages:  # пустой опрос — самый частый, соседа за пометками в нём спрашивать незачем
        _mark_breaks(messages, _neighbour(membership.chat, messages[0].pk))
    return render(request, "chats/_messages.html", {
        "chat_messages": messages,
        "updated_messages": [_mark_one(membership.chat, one) for one in updated],
        "chat": membership.chat,
        "is_chat_admin": membership.is_admin,
        "read_upto": _read_upto(membership.chat, request.user),
    })


def messages_older(request, pk):
    membership = _membership(request, pk)
    messages, has_more = _history_page(membership.chat, _int(request.GET.get("before")))
    return render(request, "chats/_history.html", {
        "chat_messages": messages,
        "has_more": has_more,
        "read_upto": _read_upto(membership.chat, request.user),
        "chat": membership.chat,
        "is_chat_admin": membership.is_admin,
    })


@require_POST
def message_send(request, pk):
    membership = _membership(request, pk)
    # Сообщение расходится по вкладкам всех участников — без предела скрипт завалит чат.
    if throttled(f"chat:send:{request.user.pk}", SEND_LIMIT, 60):
        return HttpResponse(status=429)
    text = _clean(request.POST.get("text", ""))
    photos = request.FILES.getlist("photo")
    previews = request.FILES.getlist("preview")
    docs = request.FILES.getlist("doc")
    # Пустое сообщение без вложений отправлять нечего; с вложениями — подпись не обязательна
    if not text and not photos and not docs:
        return HttpResponse(status=204)
    if refusals := problems(photos, previews, docs):
        return HttpResponse("\n".join(refusals), status=422, content_type="text/plain; charset=utf-8")

    # reply_to принимаем только из этого же чата
    reply_to = membership.chat.messages.filter(pk=_int(request.POST.get("reply_to"))).first()
    message = Message.objects.create(chat=membership.chat, author=request.user, text=text, reply_to=reply_to)
    remember(message)
    attach(message, photos, previews, docs, request.user)
    Chat.objects.filter(pk=membership.chat_id).update(last_message=message)

    # Курсор сдвинется на наш id — чужое с меньшим id отдаём сейчас, иначе оно не придёт.
    # Курсор новее нашего сообщения честно не бывает: тогда отвечаем только своим.
    after = _int(request.POST.get("after"))
    if 0 < after < message.pk:
        fresh, too_many = _catch_up(membership.chat, after)
    else:
        fresh, too_many = [message], False  # курсора нет (пустой чат) или он не наш
    _mark_read(membership, fresh)
    notify_chat(membership.chat_id, message)
    if too_many:
        return _refresh()
    return render(request, "chats/_messages.html", {
        "chat_messages": _mark_breaks(fresh, _neighbour(membership.chat, fresh[0].pk)),
        "chat": membership.chat,
        "is_chat_admin": membership.is_admin,
        "read_upto": _read_upto(membership.chat, request.user),
    })


@require_POST
def dm_start(request, user_id):
    """Только POST: заводит чат, а по ссылке ходят предзагрузчики."""
    other = get_object_or_404(User, pk=user_id, is_active=True)
    if other == request.user:
        return redirect("chat_list")
    chat = Chat.get_or_create_dm(request.user, other)
    notify_joined(other.pk, chat.pk)
    return redirect("chat_detail", pk=chat.pk)


def unread_badge(request):
    return render(request, "chats/_unread_badge.html", {"unread_total": unread_total(request.user)})


def user_search(request):
    q = request.GET.get("q", "").strip()
    return render(request, "chats/_user_search.html", {"found_users": _found_users(request.user, q)})


def chat_list_fragment(request):
    return render(request, "chats/_chat_list.html", {
        "items": _chat_items(request.user),
        "active_id": _int(request.GET.get("active")),
    })


def message_search(request, pk):
    """Поиск по сообщениям чата — подстрокой: ищут слово, которое точно писали. Удалённые
    не находятся."""
    membership = _membership(request, pk)
    query = request.GET.get("q", "").strip()
    found = []
    if len(query) >= SEARCH_MIN:
        found = list(
            membership.chat.messages.filter(text__icontains=query, deleted=False)
            .select_related("author")
            .order_by("-id")[:SEARCH_LIMIT]
        )
    return render(request, "chats/_search_results.html", {
        "found": found, "query": query, "chat": membership.chat,
        "short": 0 < len(query) < SEARCH_MIN,
        "limit_hit": len(found) == SEARCH_LIMIT,
    })


def chat_people(request, pk):
    """Участники чата для подсказки упоминаний — JSON одним списком (ищет вкладка). Себя не отдаём."""
    membership = _membership(request, pk)
    people = (
        User.objects.filter(chat_memberships__chat=membership.chat)
        .exclude(pk=request.user.pk)
        .order_by("surname", "name")
    )
    return JsonResponse([{"id": p.pk, "name": p.full_name} for p in people], safe=False)


def _chat_state(request, membership):
    """Ответ на закрепление и звук: строка списка и пункт меню — oob-заменой."""
    return render(request, "chats/_chat_state.html", {
        "items": _chat_items(request.user),
        "active_id": membership.chat_id,
        "membership": membership,
        "chat": membership.chat,
        # Нужен пункту «покинуть»: из чата СВОЕГО курса выхода нет
        "own_course": membership.chat.is_own_course(request.user),
    })


@require_POST
def chat_pin(request, pk):
    membership = _membership(request, pk)
    membership.pinned = not membership.pinned
    membership.save(update_fields=["pinned"])
    return _chat_state(request, membership)


@require_POST
def chat_mute(request, pk):
    membership = _membership(request, pk)
    membership.muted = not membership.muted
    membership.save(update_fields=["muted"])
    answer = _chat_state(request, membership)
    # Беззвучные вне общего счётчика — пусть бейдж пересчитает сам.
    answer["HX-Trigger"] = "chats:recount"
    return answer


def _system_message(chat, text):
    """Служебная строка в ленте — сообщение без автора."""
    message = Message.objects.create(chat=chat, text=text)
    Chat.objects.filter(pk=chat.pk).update(last_message=message)
    notify_chat(chat.pk, message)


@require_POST
def chat_add_members(request, pk):
    membership = _membership(request, pk)
    chat = membership.chat
    if chat.kind == "group" and membership.is_admin:
        form, verb, as_admin = AddMembersForm(request.POST, chat=chat), "добавил(а):", False
    elif chat.kind == "course":
        # Куратора зовёт любой участник курса (форма пропустит только кураторов); куратор сразу админ.
        form, verb, as_admin = CuratorAddForm(request.POST, chat=chat), "добавил(а) куратора:", True
    else:
        return HttpResponse(status=403)
    if form.is_valid() and form.cleaned_data["members"]:
        users = list(form.cleaned_data["members"])
        Membership.objects.bulk_create(
            [Membership(chat=chat, user=u, is_admin=as_admin) for u in users], ignore_conflicts=True
        )
        for user in users:
            notify_joined(user.pk, chat.pk)
        names = ", ".join(f"{u.name} {u.surname}" for u in users)
        _system_message(chat, f"{request.user.name} {request.user.surname} {verb} {names}")
    return redirect("chat_detail", pk=pk)


@require_POST
def chat_rename(request, pk):
    membership = _membership(request, pk)
    chat = membership.chat
    if chat.kind != "group" or not membership.is_admin:
        return HttpResponse(status=403)
    title = request.POST.get("title", "").strip()[:100]
    if title and title != chat.title:
        chat.title = title
        chat.save(update_fields=["title"])
        _system_message(chat, f"{request.user.name} {request.user.surname} переименовал(а) группу в «{title}»")
    return redirect("chat_detail", pk=pk)


@require_POST
def chat_remove_member(request, pk, user_id):
    membership = _membership(request, pk)
    chat = membership.chat
    if chat.kind != "group" or not membership.is_admin or user_id == request.user.pk:
        return HttpResponse(status=403)
    removed = chat.memberships.filter(user_id=user_id).select_related("user").first()
    if removed and removed.is_admin:
        return HttpResponse(status=403)  # админы равны: один не выгоняет другого
    if removed:
        removed.delete()
        notify_left(user_id, chat.pk)
        _system_message(chat, f"{request.user.name} {request.user.surname} исключил(а) {removed.user.name} {removed.user.surname}")
    return redirect("chat_detail", pk=pk)


@require_POST
def chat_leave(request, pk):
    membership = _membership(request, pk)
    chat = membership.chat
    # из ЛС и чата СВОЕГО курса выйти нельзя; куратор из чужого курсового — может
    if chat.kind == "dm" or (chat.kind == "course" and chat.is_own_course(request.user)):
        return HttpResponse(status=403)
    membership.delete()
    notify_left(request.user.pk, chat.pk)
    who = f"{request.user.name} {request.user.surname}"
    # свежий запрос: chat.memberships здесь отвечал бы из prefetch-кэша, где участник ещё «жив»
    rest = Membership.objects.filter(chat=chat)
    if chat.kind == "course":
        _system_message(chat, f"{who} покинул(а) чат")
    elif rest.exists():
        _system_message(chat, f"{who} покинул(а) группу")
        _ensure_admin(chat, rest)
    else:
        chat.delete()  # последний вышел — пустую группу не храним
    django_messages.success(request, f"Вы покинули «{chat.title}»")
    return redirect("chat_list")


def _ensure_admin(chat, memberships):
    """Ушёл единственный админ — группа осталась бы без управления навсегда."""
    if memberships.filter(is_admin=True).exists():
        return
    heir = memberships.select_related("user").order_by("joined", "id").first()
    heir.is_admin = True
    heir.save(update_fields=["is_admin"])
    _system_message(chat, f"{heir.user.name} {heir.user.surname} теперь администратор группы")


@require_POST
def chat_delete(request, pk):
    membership = _membership(request, pk)
    chat = membership.chat
    # ЛС может удалить любой из двоих (у обоих), группу — только админ, курс — никто
    if chat.kind == "course" or (chat.kind == "group" and not membership.is_admin):
        return HttpResponse(status=403)
    # Участники — пока чат существует. Каждому лично, а не в группу: иначе сокет остался бы
    # в группе удалённого чата.
    chat_id, members = chat.pk, list(chat.memberships.values_list("user_id", flat=True))
    chat.delete()
    for user_id in members:
        notify_left(user_id, chat_id)
    django_messages.success(request, "Чат удалён")
    return redirect("chat_list")


def chat_create_group(request):
    if request.method != "POST":
        return redirect("chat_list")
    form = GroupChatForm(request.POST, creator=request.user)
    if not form.is_valid():
        # список с открытой модалкой и ошибками формы
        return render(request, "chats/chat.html", _page_context(request, group_form=form, modal_open=True))
    chat = Chat.objects.create(kind="group", title=form.cleaned_data["title"])
    members = list(form.cleaned_data["members"])
    Membership.objects.bulk_create(
        [Membership(chat=chat, user=request.user, is_admin=True)]
        + [Membership(chat=chat, user=u) for u in members]
    )
    for user in members:
        notify_joined(user.pk, chat.pk)
    django_messages.success(request, f"Группа «{chat.title}» создана")
    return redirect("chat_detail", pk=chat.pk)


def message_card(request, pk):
    """Пузырь целиком — цель для «Отмены» редактирования."""
    return _bubble(request, _message(request, pk))


def message_edit(request, pk):
    message = _message(request, pk)
    if message.author_id != request.user.pk or message.deleted:
        return HttpResponse(status=403)
    if request.method == "POST":
        if _too_fast(request):
            return HttpResponse(status=429)
        text = _clean(request.POST.get("text", ""))
        if text and text != message.text:
            message.text = text
            message.edited = timezone.now()
            message.updated = timezone.now()
            message.save(update_fields=["text", "edited", "updated"])
            remember(message)  # позвали кого-то правкой — или, наоборот, убрали
            notify_chat(message.chat_id, kind="edit")
        return _bubble(request, message)
    return render(request, "chats/_message_edit.html", {"m": message, "max_text": MAX_TEXT})


@require_POST
def message_delete(request, pk):
    message = _message(request, pk)
    is_chat_admin = message.chat.memberships.filter(user=request.user, is_admin=True).exists()
    if message.author_id != request.user.pk and not is_chat_admin:
        return HttpResponse(status=403)
    if _too_fast(request):
        return HttpResponse(status=429)
    message.deleted = True
    message.save(update_fields=["deleted"])
    _touch(message, "delete")
    return _bubble(request, message)


def message_readers(request, pk):
    """Кто прочитал моё сообщение — по нажатию: живые галочки в группе — лавина событий."""
    message = _message(request, pk)
    if message.author_id != request.user.pk:
        return HttpResponse(status=403)
    seen = (
        Membership.objects.filter(chat_id=message.chat_id, last_read_id__gte=message.pk)
        .exclude(user_id=request.user.pk)
        .select_related("user__team")
        .order_by("user__surname", "user__name")
    )
    return render(request, "chats/_readers.html", {
        "readers": [ms.user for ms in seen],
        "total": message.chat.memberships.count() - 1,  # без меня
        # В ЛС отвечаем «да» или «нет», а не списком из одного.
        "alone": message.chat.kind == "dm",
    })


@require_POST
def message_react(request, pk):
    message = _message(request, pk)
    if _too_fast(request):
        return HttpResponse(status=429)
    emoji = request.POST.get("emoji", "")
    if emoji in REACTIONS and not message.deleted:
        # get_or_create: два быстрых нажатия иначе падали на уникальном индексе.
        _, added = message.reactions.get_or_create(user=request.user, emoji=emoji)
        if not added:
            message.reactions.filter(user=request.user, emoji=emoji).delete()
        _touch(message, "react")
        message = _message(request, pk)  # свежие реакции после изменения
    return _bubble(request, message)
