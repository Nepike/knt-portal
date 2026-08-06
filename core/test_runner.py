import tempfile

from django.core.files.storage import FileSystemStorage
from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class MediaIsolatedRunner(DiscoverRunner):
    """Тесты пишут файлы во временный каталог и НИКОГДА в R2.

    Хранилище у FileField выбирается один раз при импорте моделей, поэтому
    override_settings его уже не переубедит — подменяем объект прямо в поле.
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        from attachments.models import File, Image

        self._media = tempfile.TemporaryDirectory()
        self._override = override_settings(MEDIA_ROOT=self._media.name)
        self._override.enable()

        storage = FileSystemStorage(location=self._media.name)
        for model, field in ((File, "file"), (Image, "image")):
            model._meta.get_field(field).storage = storage

    def teardown_test_environment(self, **kwargs):
        super().teardown_test_environment(**kwargs)
        self._override.disable()
        self._media.cleanup()
