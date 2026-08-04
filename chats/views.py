from datetime import timedelta

from django.contrib import messages as django_messages
from django.db.models import F, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from users.models import User

from .events import notify_chat, notify_joined, notify_left
from .forms import AddMembersForm, CuratorAddForm, GroupChatForm
from .models import REACTIONS, Chat, Membership, Message, unread_total

MESSAGE_RELATIONS = ("author", "reply_to", "reply_to__author")
PAGE_SIZE = 30  # сообщений в порции истории
MAX_TEXT = 4000  # столько же в maxlength поля ввода


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
        .prefetch_related("chat__memberships__user")
        .order_by(F("chat__last_message_id").desc(nulls_last=True))
    )
    for item in items:
        item.other = item.chat.other_member(user)  # None для групп
    return items


def _membership(request, pk):
    """Он же проверка доступа: не участник — 404. Намеренно один запрос:
    фрагментам и действиям состав чата не нужен."""
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


def _message(request, pk):
    """Он же проверка доступа: не участник чата — 404."""
    return get_object_or_404(
        Message.objects.select_related("chat", *MESSAGE_RELATIONS).prefetch_related("reactions"),
        pk=pk,
        chat__memberships__user=request.user,
    )


def _history_page(chat, before=0):
    """Порция истории: PAGE_SIZE сообщений старше `before` (0 — самые свежие)."""
    qs = chat.messages.select_related(*MESSAGE_RELATIONS).prefetch_related("reactions")
    if before:
        qs = qs.filter(id__lt=before)
    page = list(qs.order_by("-id")[: PAGE_SIZE + 1])  # +1 — чтобы узнать, есть ли ещё
    return list(reversed(page[:PAGE_SIZE])), len(page) > PAGE_SIZE


def _bubble(request, message):
    is_chat_admin = message.chat.memberships.filter(user=request.user, is_admin=True).exists()
    return render(request, "chats/_message.html", {"m": message, "chat": message.chat, "is_chat_admin": is_chat_admin})


def _touch(message):
    """Пометить контент изменённым: messages_new отдаст пузырь oob-заменой."""
    Message.objects.filter(pk=message.pk).update(updated=timezone.now())
    notify_chat(message.chat_id)


def _found_users(user, q=""):
    qs = User.objects.filter(is_active=True).exclude(pk=user.pk).select_related("team").order_by("surname", "name")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(surname__icontains=q) | Q(patronymic__icontains=q))
    return qs[:10]


def _page_context(request, **extra):
    return {
        "items": _chat_items(request.user),
        "found_users": _found_users(request.user),
        "group_form": extra.pop("group_form", None) or GroupChatForm(creator=request.user),
        **extra,
    }


def chat_list(request):
    return render(request, "chats/chat.html", _page_context(request))


def chat_detail(request, pk):
    membership = _membership_page(request, pk)
    messages, has_more = _history_page(membership.chat)  # только последняя страница
    _mark_read(membership, messages)
    context = _page_context(
        request,
        chat=membership.chat,
        membership=membership,
        active_id=membership.chat_id,
        is_chat_admin=membership.is_admin,
        other=membership.chat.other_member(request.user),  # для шапки ЛС
        # НЕ "messages" — имя занято django.contrib.messages (тосты в base.html)
        chat_messages=messages,
        has_more=has_more,
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
    qs = membership.chat.messages.select_related(*MESSAGE_RELATIONS).prefetch_related("reactions")
    messages = list(qs.filter(id__gt=after))
    # правки/удаления/реакции последних секунд; повторная oob-замена безвредна, окно берём с запасом
    # TODO: правку старше окна пропустившая вкладка не увидит — нужен второй курсор, по updated
    updated = list(qs.filter(id__lte=after, updated__gte=timezone.now() - timedelta(seconds=12))) if after else []
    _mark_read(membership, messages)
    return render(request, "chats/_messages.html", {
        "chat_messages": messages,
        "updated_messages": updated,
        "chat": membership.chat,
        "is_chat_admin": membership.is_admin,
    })


def messages_older(request, pk):
    membership = _membership(request, pk)
    messages, has_more = _history_page(membership.chat, _int(request.GET.get("before")))
    return render(request, "chats/_history.html", {
        "chat_messages": messages,
        "has_more": has_more,
        "chat": membership.chat,
        "is_chat_admin": membership.is_admin,
    })


@require_POST
def message_send(request, pk):
    membership = _membership(request, pk)
    text = request.POST.get("text", "").strip()[:MAX_TEXT]
    if not text:
        return HttpResponse(status=204)

    # reply_to принимаем только из этого же чата
    reply_to = membership.chat.messages.filter(pk=_int(request.POST.get("reply_to"))).first()
    message = Message.objects.create(chat=membership.chat, author=request.user, text=text, reply_to=reply_to)
    Chat.objects.filter(pk=membership.chat_id).update(last_message=message)

    # Курсор ленты сдвинется на наш id, поэтому чужое сообщение с меньшим id
    # отдаём прямо сейчас — иначе оно не придёт уже никогда.
    after = _int(request.POST.get("after"))
    fresh = (
        list(membership.chat.messages.select_related(*MESSAGE_RELATIONS).prefetch_related("reactions").filter(id__gt=after))
        if after else [message]  # курсора нет (пустой чат) — отдаём только своё, не всю историю
    )
    _mark_read(membership, fresh)
    notify_chat(membership.chat_id)
    return render(request, "chats/_messages.html", {
        "chat_messages": fresh,
        "chat": membership.chat,
        "is_chat_admin": membership.is_admin,
    })


def dm_start(request, user_id):
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


def _system_message(chat, text):
    """Служебная строка в ленте — сообщение без автора."""
    message = Message.objects.create(chat=chat, text=text)
    Chat.objects.filter(pk=chat.pk).update(last_message=message)
    notify_chat(chat.pk)


@require_POST
def chat_add_members(request, pk):
    membership = _membership(request, pk)
    chat = membership.chat
    if chat.kind == "group" and membership.is_admin:
        form, verb, as_admin = AddMembersForm(request.POST, chat=chat), "добавил(а):", False
    elif chat.kind == "course":
        # позвать куратора может любой участник курса — форма пропустит только их;
        # куратор и есть модератор курсового чата, поэтому сразу админ
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
    notify_chat(chat.pk)  # пока чат ещё существует — иначе некому будет сказать
    chat.delete()
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
        text = request.POST.get("text", "").strip()[:MAX_TEXT]
        if text and text != message.text:
            message.text = text
            message.edited = timezone.now()
            message.updated = timezone.now()
            message.save(update_fields=["text", "edited", "updated"])
            notify_chat(message.chat_id)
        return _bubble(request, message)
    return render(request, "chats/_message_edit.html", {"m": message})


@require_POST
def message_delete(request, pk):
    message = _message(request, pk)
    is_chat_admin = message.chat.memberships.filter(user=request.user, is_admin=True).exists()
    if message.author_id != request.user.pk and not is_chat_admin:
        return HttpResponse(status=403)
    message.deleted = True
    message.save(update_fields=["deleted"])
    _touch(message)
    return _bubble(request, message)


@require_POST
def message_react(request, pk):
    message = _message(request, pk)
    emoji = request.POST.get("emoji", "")
    if emoji in REACTIONS and not message.deleted:
        existing = message.reactions.filter(user=request.user, emoji=emoji)
        if existing.exists():
            existing.delete()
        else:
            message.reactions.create(user=request.user, emoji=emoji)
        _touch(message)
        message = _message(request, pk)  # свежие реакции после изменения
    return _bubble(request, message)
