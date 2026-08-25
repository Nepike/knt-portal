import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.views import (
    PasswordChangeView as BasePasswordChangeView,
    PasswordResetConfirmView as BasePasswordResetConfirmView,
    PasswordResetView as BasePasswordResetView,
)
from django.contrib.sessions.models import Session
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cosmetics.models import CosmeticItem
from cosmetics.services import inventory, outfit
from core.models import Moderated
from core.throttle import client_ip, throttled
from library.models import Book
from materials.models import Material

from .forms import MAX_AVATAR_DATA, ProfileForm, RegisterUserForm
from .models import User
from .sessions import alive


def _by_term(materials):
    """Материалы по семестрам. Материал с двумя семестрами попадает в оба — так и задумано:
    это разбивка «когда пригодится», а не деление одной работы на части.

    order_by() перед values() обязателен: у Material в Meta стоит сортировка по дате,
    а сортировка попадает в GROUP BY — без сброса каждый материал стал бы своей группой.
    """
    rows = list(
        materials.order_by().values("terms__number").annotate(count=Count("id")).order_by("terms__number")
    )
    top = max((row["count"] for row in rows), default=0)
    return [
        {
            "label": f"{row['terms__number']} семестр" if row["terms__number"] else "Без семестра",
            "count": row["count"],
            "share": round(row["count"] * 100 / top),
        }
        for row in rows
    ]


def _contributions(person, full):
    """Вклад человека в сайт. Считаем только опубликованное: материал на проверке ещё
    может и не выйти, а в профиле он выглядел бы как заслуга.

    full — свой профиль. В чужом анонимные работы не показываем даже числом: у того,
    у кого такая работа одна, счётчик и есть та самая подпись, которую он снял.
    """
    approved = Moderated.Status.APPROVED
    materials = Material.objects.filter(uploader=person, status=approved)
    books = Book.objects.filter(uploader=person, status=approved)
    reviews = person.teacher_reviews.all()
    comments = person.material_comments.all()
    if not full:
        materials = materials.filter(hide_uploader=False)
        books = books.filter(hide_uploader=False)
        reviews = reviews.filter(hide_author=False)
        comments = comments.filter(hide_author=False)

    return {
        "materials": materials.count(),
        "books": books.count(),
        "reviews": reviews.count(),
        "comments": comments.count(),
        "by_term": _by_term(materials),
    }


RECENT = 7  # операций в кошельке на странице профиля, дальше — «вся история»


def profile(request, pk):
    # TODO (M5): значки — когда будет что рисовать
    person = get_object_or_404(
        User.objects.select_related("team", "wallet"), pk=pk, is_active=True,
    )
    own = person.pk == request.user.pk
    wallet = getattr(person, "wallet", None)
    on = outfit(person)
    # Берём на одну больше, чем покажем: так видно, есть ли что смотреть дальше,
    # и это тот же запрос, а не второй ради count().
    entries = list(wallet.entries.all()[: RECENT + 1]) if wallet and own else []
    return render(request, "users/profile.html", {
        "person": person,
        "own": own,
        "stats": _contributions(person, full=own),
        # История трат и список устройств — только свои: по ним видно и что человек
        # покупал, и откуда он заходит.
        "entries": entries[:RECENT],
        "more": len(entries) > RECENT,
        "devices": _devices(request) if own else [],
        "worn": on.get(CosmeticItem.Kind.AVATAR_FRAME),
        "header": on.get(CosmeticItem.Kind.PROFILE_HEADER),
        "background": on.get(CosmeticItem.Kind.PROFILE_BACKGROUND),
        # Инвентарь — только свой: чужой сундук это витрина «вот чего у тебя нет».
        "items": inventory(person) if own else [],
    })


def _devices(request):
    """Сессии для страницы. Ключ сессии наружу не отдаём даже своему хозяину: это пароль
    на предъявителя, и попавший на скриншот действует до конца срока. Наружу — id строки."""
    mine = request.session.session_key
    return [(row, row.session_id == mine) for row in alive(request.user)]


@require_POST
def session_end(request):
    """Закрыть сессию: своя строка по id в теле запроса, без него — все, кроме текущей.

    Id в теле, а не в адресе: адреса оседают в логах и в Referer, а тут закрывается доступ.
    Текущую не трогаем — это просто выход, и он рядом, в меню аккаунта.
    """
    doomed = alive(request.user).exclude(session_id=request.session.session_key)
    pk = request.POST.get("id", "")
    if pk:
        # Не число — значит, запрос пришёл не с нашей страницы. Молча закрывать всё
        # в таком случае нельзя, а падать пятисоткой на кривом поле незачем.
        if not pk.isdigit():
            return redirect("profile", pk=request.user.pk)
        doomed = doomed.filter(pk=pk)

    # Удаляем сами сессии — записи уедут за ними каскадом.
    closed = Session.objects.filter(pk__in=doomed.values("session_id")).delete()[0]
    messages.success(request, f"Закрыто сессий: {closed}" if closed else "Закрывать нечего")
    return redirect("profile", pk=request.user.pk)


def profile_edit(request):
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user)
    was = request.user.photo
    if request.method == "POST" and form.is_valid():
        form.save()
        # Прежний блоб больше ничей: запись жива, а добраться до него потом неоткуда.
        if was and str(was) != str(request.user.photo):
            was.storage.delete(str(was))
        messages.success(request, "Профиль обновлён")
        return redirect("profile", pk=request.user.pk)

    return render(request, "users/profile_edit.html", {"form": form, "avatar_limit": MAX_AVATAR_DATA})


def _clear_must_change_password(user):
    if user.must_change_password:
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])


class PasswordChangeView(BasePasswordChangeView):
    template_name = "users/password_change.html"
    success_url = settings.LOGIN_REDIRECT_URL

    def form_valid(self, form):
        response = super().form_valid(form)
        _clear_must_change_password(self.request.user)
        messages.success(self.request, "Пароль обновлён")
        return response


class PasswordResetView(BasePasswordResetView):
    template_name = "users/password_reset.html"
    email_template_name = "users/password_reset_email.txt"
    subject_template_name = "users/password_reset_subject.txt"

    def form_valid(self, form):
        # Защита от спама письмами: при превышении лимита молча уходим на done-страницу.
        ip = client_ip(self.request)
        email = form.cleaned_data["email"].lower()
        if throttled(f"pwreset:ip:{ip}", 5) or throttled(f"pwreset:email:{email}", 3):
            return HttpResponseRedirect(self.get_success_url())
        return super().form_valid(form)


class PasswordResetConfirmView(BasePasswordResetConfirmView):
    template_name = "users/password_reset_confirm.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        _clear_must_change_password(form.user)
        return response


@permission_required("users.add_user", raise_exception=True)
def user_new(request):
    form = RegisterUserForm(request.POST or None, creator=request.user)
    if request.method == "POST" and form.is_valid():
        user = form.save(commit=False)
        user.set_password(secrets.token_urlsafe(16))  # пароля не знает никто — студент задаст свой по ссылке
        user.must_change_password = False
        user.save()
        form.save_m2m()

        mail_form = PasswordResetForm({"email": user.email})
        mail_form.is_valid()
        mail_form.save(
            request=request,
            email_template_name="users/welcome_email.txt",
            subject_template_name="users/welcome_subject.txt",
        )
        messages.success(request, f"Аккаунт создан, письмо отправлено на {user.email}")
        return redirect("user_new")

    return render(request, "users/user_new.html", {"form": form})
