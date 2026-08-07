from django.http import HttpResponseForbidden
from django.shortcuts import render

from .queue import allowed, pending


def review_queue(request):
    if not allowed(request.user):
        return HttpResponseForbidden("Эта страница только для модерации.")
    return render(request, "moderation/queue.html", {"groups": pending(request.user)})
