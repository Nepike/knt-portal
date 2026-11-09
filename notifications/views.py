from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST, require_safe

from . import services
from .models import Announcement, Notification

PANEL = 8
PAGE = 30


def _mine(user):
    return Notification.objects.filter(recipient=user).select_related(
        "actor", "comment", "material", "book", "playlist", "lecture__playlist", "announcement",
    )


@require_safe
def notification_panel(request):
    """Панель под колокольчиком: грузится при каждом открытии, поэтому всегда свежая."""
    return render(request, "notifications/_panel.html", {"items": _mine(request.user)[:PANEL]})


@require_safe
def notification_badge(request):
    """Один счётчик — его переспрашивает вкладка, когда на неё возвращаются."""
    return render(request, "notifications/_badge.html", {"unread_notifications": services.unread(request.user).count()})


@require_safe
def notification_list(request):
    page = Paginator(_mine(request.user), PAGE).get_page(request.GET.get("page"))
    return render(request, "notifications/list.html", {"page": page})


def notification_open(request, pk):
    """Ссылка из уведомления: отметить прочитанным и отвести на место. GET, а не форма:
    это обычная ссылка — её открывают в новой вкладке и колёсиком."""
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    services.opened(notification)
    return redirect(notification.target_url())


@require_safe
def announcement_detail(request, pk):
    """Объявление целиком. Видят его те, кому разослали, и те, кто рассылает."""
    announcement = get_object_or_404(Announcement.objects.select_related("author"), pk=pk)
    mine = announcement.notifications.filter(recipient=request.user).exists()
    if not mine and not request.user.has_perm("notifications.add_announcement"):
        raise Http404
    services.seen(request.user, announcement=announcement)
    return render(request, "notifications/announcement.html", {"announcement": announcement})


@require_POST
def notification_read_all(request):
    services.read_all(request.user)
    if request.headers.get("HX-Request"):
        return render(request, "notifications/_panel.html", {"items": _mine(request.user)[:PANEL], "cleared": True})
    return redirect("notification_list")
