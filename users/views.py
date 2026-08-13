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
from django.core.cache import cache
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render

from .forms import RegisterUserForm
from .models import User


def profile(request, pk):
    # TODO (M5 profiles): косметика, уровень, значки, инвентарь
    person = get_object_or_404(User.objects.select_related("team", "wall"), pk=pk, is_active=True)
    return render(request, "users/profile.html", {"person": person})


def _clear_must_change_password(user):
    if user.must_change_password:
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])


def _throttled(key, limit, window=3600):
    """Фиксированное окно на кэше: True — лимит исчерпан."""
    if cache.add(key, 1, window):
        return False
    try:
        return cache.incr(key) > limit
    except ValueError:
        cache.set(key, 1, window)
        return False


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "")


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
        ip = _client_ip(self.request)
        email = form.cleaned_data["email"].lower()
        if _throttled(f"pwreset:ip:{ip}", 5) or _throttled(f"pwreset:email:{email}", 3):
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
