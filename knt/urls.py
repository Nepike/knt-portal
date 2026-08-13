from django.contrib import admin
from django.urls import include, path
from django.conf.urls.static import static
from django.conf import settings

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('core.urls')),
    path('', include('users.urls')),
    path('', include('teachers.urls')),
    path('', include('chats.urls')),
    path('', include('library.urls')),
    path('', include('materials.urls')),
    path('', include('attachments.urls')),
    path('', include('moderation.urls')),
    path('', include('wall.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
