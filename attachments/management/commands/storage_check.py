from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from attachments.storage import file_storage, media_storage

PAYLOAD = b"knt storage check"
# Сколько записей сверить с хранилищем. Больше не нужно: расходится обычно ВСЁ разом —
# не тот бакет, не тот префикс, — а не отдельные ключи.
SAMPLE = 5


class Command(BaseCommand):
    help = "Проверяет хранилище файлов: запись, чтение, ссылку, удаление и что данные на месте."

    def handle(self, *args, **options):
        storage = media_storage()
        self.stdout.write(f"хранилище: {type(storage).__name__}")
        if settings.R2_BUCKET:
            self.stdout.write(f"бакет: {settings.R2_BUCKET} на {settings.R2_OPTIONS['endpoint_url']}")
        prefix = getattr(storage, "location", "")
        self.stdout.write(f"префикс ключей: {prefix or '(нет)'}")

        name = storage.save("checks/storage-check.txt", ContentFile(PAYLOAD))
        try:
            self.stdout.write(f"записан: {name}")
            with storage.open(name) as saved:
                body = saved.read()
            if body != PAYLOAD:
                raise CommandError(f"прочиталось не то, что писали: {body!r}")
            self.stdout.write(f"прочитан: {len(body)} байт")
            self.stdout.write(f"ссылка: {storage.url(name)}")
        finally:
            storage.delete(name)

        self.stdout.write(self.style.SUCCESS("хранилище работает"))
        self.check_data(prefix)

    def check_data(self, prefix):
        """Записать и прочитать своё — мало.

        Хранилище отвечает и когда смотрит не туда: сменили бакет, дописали R2_PREFIX —
        новые ключи ложатся и читаются прекрасно, а всё, что уже есть в базе, разом
        пропадает. Снаружи это выглядит как «сломались все картинки», и виновата
        настройка, а не код. Поэтому сверяемся с тем, что база СЧИТАЕТ существующим.
        """
        from attachments.storage import media_fields

        storage = file_storage()
        checked = missing = 0
        for model, field in media_fields():
            # Пустое поле бывает двух видов: "" и NULL (фото у пользователя — null=True).
            rows = (
                model.objects.exclude(**{field: ""}).exclude(**{f"{field}__isnull": True})
                .values_list(field, flat=True).order_by("-pk")[:SAMPLE]
            )
            for key in rows:
                checked += 1
                if not storage.exists(key):
                    missing += 1
                    if missing <= 3:
                        self.stdout.write(self.style.ERROR(f"  нет в хранилище: {key}"))

        if not checked:
            self.stdout.write("в базе нет ни одного файла — сверять нечего")
            return
        if not missing:
            self.stdout.write(self.style.SUCCESS(f"данные на месте: проверено {checked} записей"))
            return

        self.stdout.write(self.style.ERROR(f"\nне найдено {missing} из {checked} проверенных"))
        if missing == checked:
            self.stdout.write(self.style.WARNING(
                "Пропало ВСЁ разом — это почти всегда настройка, а не потеря данных.\n"
                f"Сейчас ключи ищутся с префиксом {prefix or '(нет)'} в бакете {settings.R2_BUCKET or '(диск)'}.\n"
                "Проверь R2_PREFIX и R2_BUCKET в .env: в базе ключ лежит БЕЗ префикса,\n"
                "его дописывает хранилище, и смена префикса уводит от всех старых файлов."
            ))
        raise CommandError("хранилище отвечает, но данных в нём нет")
