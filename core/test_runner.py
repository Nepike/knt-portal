import tempfile

from django.apps import apps
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class MediaIsolatedRunner(DiscoverRunner):
    """Тесты пишут файлы во временный каталог, никогда в R2. Хранилище FileField выбрано
    при импорте моделей — подменяем его в полях ВСЕХ моделей, чтобы не забыть новое поле."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        from knt.celery import app as celery_app

        # Задачи выполняются на месте; propagates — упавшая задача валит тест.
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True

        self._media = tempfile.TemporaryDirectory()
        # Обычный вид сайта в любой месяц: скин тест включает сам.
        self._override = override_settings(MEDIA_ROOT=self._media.name, SITE_THEME="default")
        self._override.enable()

        storage = FileSystemStorage(location=self._media.name)
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if isinstance(field, models.FileField):
                    field.storage = storage

    def teardown_test_environment(self, **kwargs):
        super().teardown_test_environment(**kwargs)
        self._override.disable()
        self._media.cleanup()
