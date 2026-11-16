from django.contrib import admin
from django.contrib.auth.decorators import login_not_required
from django.urls import include, path
from django.conf.urls.static import static
from django.conf import settings
from django.views.static import serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('core.urls')),
    path('', include('users.urls')),
    path('', include('teachers.urls')),
    path('', include('chats.urls')),
    path('', include('library.urls')),
    path('', include('materials.urls')),
    path('', include('comments.urls')),
    path('', include('bookmarks.urls')),
    path('', include('attachments.urls')),
    path('', include('intake.urls')),
    path('', include('lectorium.urls')),
    path('', include('moderation.urls')),
    path('', include('cosmetics.urls')),
    path('', include('economy.urls')),
    path('', include('wall.urls')),
    path('', include('printing.urls')),
    path('', include('pulse.urls')),
    path('', include('telegram.urls')),
    path('', include('notifications.urls')),
    path('', include('quest.urls')),
]

# Файлы с диска — только в dev (вне DEBUG static() пуст). Без входа, как домен файлов на проде.
urlpatterns += static(settings.MEDIA_URL, view=login_not_required(serve), document_root=settings.MEDIA_ROOT)
