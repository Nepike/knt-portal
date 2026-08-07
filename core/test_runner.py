import tempfile

from django.apps import apps
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class MediaIsolatedRunner(DiscoverRunner):
    """Тесты пишут файлы во временный каталог и НИКОГДА в R2.

    Хранилище у FileField выбирается один раз при импорте моделей, поэтому
    override_settings его уже не переубедит — подменяем объект прямо в поле.
    Проходим по ВСЕМ моделям, а не по списку: иначе новое файловое поле однажды
    появится, про подмену забудут, и тесты молча полезут в боевой бакет.
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        from knt.celery import app as celery_app

        # Фоновые задачи выполняем на месте: тестам не нужен ни Redis, ни воркер.
        # propagates — чтобы упавшая задача валила тест, а не тонула в логе.
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True

        self._media = tempfile.TemporaryDirectory()
        self._override = override_settings(MEDIA_ROOT=self._media.name)
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
