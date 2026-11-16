from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from attachments.models import File
from attachments.storage import drop_prefix, file_storage
from attachments.uploads import listed, under
from intake.models import MediaJob
from lectorium.models import Lecture

PREFIX = "uploads"
# Папки готовых наборов HLS: их создаёт приёмка (`intake.views.plan`).
SETS = "lectures"


class Command(BaseCommand):
    help = "Убирает из хранилища бесхозное: прямые загрузки без записи File и папки наборов без лекции."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=1, help="не трогать загруженное за последние N дней")
        parser.add_argument("--apply", action="store_true", help="без него только показывает, что удалил бы")

    def handle(self, *args, **options):
        storage = file_storage()
        cutoff = timezone.now() - timedelta(days=options["days"])
        known = self.needed()

        found = size = 0
        # Одним листингом с датой и весом: обход по папкам с вопросом о каждом файле не кончался бы.
        for key, (modified, weight) in sorted(listed(PREFIX).items()):
            if key in known or modified > cutoff:
                continue
            found += 1
            size += weight
            self.stdout.write(key)
            if options["apply"]:
                storage.delete(key)

        # Окно печатаем всегда: иначе «удалено: 0» выглядит поломкой.
        window = f"старше {options['days']} дн" if options["days"] else "любого возраста"
        verdict = "удалено" if options["apply"] else "нашлось (запусти с --apply)"
        self.stdout.write(self.style.SUCCESS(f"{verdict}: {found} ({window}), {size // 1024 // 1024} МБ"))

        broken = self.abandoned(storage, cutoff, options["apply"])
        self.stdout.write(self.style.SUCCESS(f"брошенных многочастных загрузок: {broken}"))

        stray, pieces = self.stray_sets(storage, cutoff, options["apply"])
        self.stdout.write(self.style.SUCCESS(f"бесхозных наборов: {stray} ({pieces} кусков)"))

    def needed(self):
        """Ключи с хозяином: записи `File` и сырьё лекций в незакрытых заданиях пекарни
        (у сырья записи нет, а пекарня может стоять неделю). Команда обходит всё хранилище —
        поэтому знает и чужие приложения."""
        keys = set(File.objects.filter(file__startswith=f"{PREFIX}/").values_list("file", flat=True))
        # Кроме закрытых: у готового сырьё снимает своя задача, а невышедшее могут вернуть в очередь.
        keys |= set(
            MediaJob.objects.exclude(status=MediaJob.Status.DONE).values_list("source", flat=True)
        )
        return keys

    def stray_sets(self, storage, cutoff, apply):
        """Папки готовых наборов без лекции и задания: хозяина теряют при удалении в обход
        сигналов или когда лёг Redis. Весят гигабайты, а в базе о них ни строки."""
        known = set(Lecture.objects.exclude(prefix="").values_list("prefix", flat=True))
        known |= set(MediaJob.objects.exclude(prefix="").values_list("prefix", flat=True))

        try:
            folders, _ = storage.listdir(SETS)
        except FileNotFoundError:
            return 0, 0  # локальный диск: каталога ещё нет, значит и наборов нет

        found = pieces = 0
        for folder in folders:
            prefix = f"{SETS}/{folder}"
            if prefix in known:
                continue
            # Возраст — по первому куску: у папки в бакете времени нет.
            keys = sorted(under(prefix))
            if not keys or storage.get_modified_time(keys[0]) > cutoff:
                continue
            found += 1
            self.stdout.write(f"{prefix} ({len(keys)} кусков)")
            pieces += drop_prefix(prefix) if apply else len(keys)
        return found, pieces

    def abandoned(self, storage, cutoff, apply):
        """Начатые и не собранные многочастные загрузки: их части стоят денег, а обычным
        листингом не видны."""
        client = getattr(getattr(storage, "connection", None), "meta", None)
        if client is None:
            return 0  # локальный диск: многочастных загрузок там не бывает

        client = client.client
        # Только свой префикс: бакет у dev и прода один.
        own = {"Prefix": f"{storage.location}/"} if getattr(storage, "location", "") else {}
        found, marker = 0, {}
        while True:
            answer = client.list_multipart_uploads(Bucket=storage.bucket_name, **own, **marker)
            for upload in answer.get("Uploads", []):
                if upload["Initiated"] > cutoff:
                    continue
                found += 1
                self.stdout.write(f"{upload['Key']} (начата {upload['Initiated']:%d.%m %H:%M})")
                if apply:
                    client.abort_multipart_upload(
                        Bucket=storage.bucket_name, Key=upload["Key"], UploadId=upload["UploadId"],
                    )
            if not answer.get("IsTruncated"):
                return found
            marker = {
                "KeyMarker": answer["NextKeyMarker"],
                "UploadIdMarker": answer["NextUploadIdMarker"],
            }
