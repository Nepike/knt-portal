"""Обращения из поддержки и контактов: записать, проверить гостя, отправить в чат.

Одним путём ходят и форма, и кнопка «Не спам» в админке — сообщение в чате
у них выходит одинаковым.
"""

from django.urls import reverse

from telegram.notify import FEEDBACK, SUPPORT, notify

from . import spam
from .forms import SupportForm
from .models import Inquiry
from .throttle import client_ip, throttled

FLOOD = 10  # обращений в час с одного адреса
CHATS = {
    Inquiry.Source.SUPPORT: (SUPPORT, "telegram/support.html", "support"),
    Inquiry.Source.CONTACTS: (FEEDBACK, "telegram/feedback.html", "feedback"),
}


def take(request, source, form):
    """Принять обращение из провалидированной формы. Возвращает запись или None, если
    адрес выбрал лимит: такое не пишем даже в таблицу — иначе бот, долбящий с одного
    адреса, заваливал бы её строками. Ни про лимит, ни про фильтр человеку не говорим:
    спамить в ответ подсказками незачем."""
    if throttled(f"{CHATS[source][2]}:ip:{client_ip(request)}", FLOOD):
        return None

    data = form.cleaned_data
    author = request.user if request.user.is_authenticated else None
    inquiry = Inquiry(
        source=source, author=author, topic=data.get("topic", ""), name=data.get("name", ""),
        contact=data.get("contact", ""), text=data["text"], had_image=bool(data.get("image")),
        ip=client_ip(request) or None, agent=request.headers.get("user-agent", "")[:200], seconds=form.seconds(),
    )
    # Вошедший известен по имени, проверять его незачем — фильтр только для гостей.
    if author is None:
        inquiry.score, hits = spam.score(inquiry.text, inquiry.name, inquiry.contact, inquiry.seconds)
        inquiry.reasons = [[sign, points] for sign, points in hits]
    inquiry.verdict = Inquiry.Verdict.SPAM if inquiry.score >= spam.THRESHOLD else Inquiry.Verdict.SENT
    inquiry.save()
    if inquiry.verdict == Inquiry.Verdict.SENT:
        send(request, inquiry, image=data.get("image"))
    return inquiry


def release(request, inquiry):
    """«Не спам»: отсеянное уходит в чат с пометкой, что его вернули руками."""
    inquiry.verdict = Inquiry.Verdict.RELEASED
    inquiry.save(update_fields=["verdict"])
    send(request, inquiry)


def send(request, inquiry, image=None):
    chat, template, _ = CHATS[inquiry.source]
    author = inquiry.author
    notify(chat, template, {
        "text": inquiry.text,
        # В записи код («broken»), а в чат нужно словами
        "topic": dict(SupportForm.TOPICS).get(inquiry.topic, inquiry.topic),
        "name": inquiry.name,
        "contact": inquiry.contact,
        "author": author,
        # Кто человек и как с ним связаться — на его странице; почту в чат не тащим.
        "profile_url": request.build_absolute_uri(reverse("profile", args=[author.pk])) if author else "",
        "inquiry": inquiry,
    }, image=image)
