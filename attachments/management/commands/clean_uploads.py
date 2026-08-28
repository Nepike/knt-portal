from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from attachments.models import File
from attachments.storage import drop_prefix, file_storage
from attachments.uploads import under
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

        try:
            folders, _ = storage.listdir(PREFIX)
        except FileNotFoundError:
            folders = []  # локальный диск: каталога ещё нет, значит и сирот нет

        found = size = 0
        for folder in folders:
            for name in storage.listdir(f"{PREFIX}/{folder}")[1]:
                key = f"{PREFIX}/{folder}/{name}"
                if key in known or storage.get_modified_time(key) > cutoff:
                    continue
                found += 1
                size += storage.size(key)
                self.stdout.write(key)
                if options["apply"]:
                    storage.delete(key)

        # Окно печатаем всегда: без него «удалено: 0» выглядит поломкой, хотя сироты
        # просто моложе --days (свежую загрузку ещё может подобрать открытая форма).
        window = f"старше {options['days']} дн" if options["days"] else "любого возраста"
        verdict = "удалено" if options["apply"] else "нашлось (запусти с --apply)"
        self.stdout.write(self.style.SUCCESS(f"{verdict}: {found} ({window}), {size // 1024 // 1024} МБ"))

        broken = self.abandoned(storage, cutoff, options["apply"])
        self.stdout.write(self.style.SUCCESS(f"брошенных многочастных загрузок: {broken}"))

        stray, pieces = self.stray_sets(storage, cutoff, options["apply"])
        self.stdout.write(self.style.SUCCESS(f"бесхозных наборов: {stray} ({pieces} кусков)"))

    def needed(self):
        """Ключи, которые нельзя трогать: у них есть хозяин.

        Не только записи `File`. Сырьё лекции хозяина в виде записи не имеет вовсе —
        оно живёт ключом в задании и ждёт, когда за ним придёт пекарня. Пекарня может
        стоять выключенной неделю, а «старше суток и не привязан к File» — это ровно
        описание такого сырья: без этой половины уборка сносила бы его из-под очереди,
        и лекция падала бы с «нет такого файла» вместо того, чтобы испечься.

        Импорт чужого приложения тут уместен, а в `attachments/uploads.py` — нет:
        библиотека вложений про лекторий знать не должна, а команда уборки по своей
        сути обходит ВСЁ хранилище и обязана знать всех, кто в нём что-то держит.
        """
        keys = set(File.objects.filter(file__startswith=f"{PREFIX}/").values_list("file", flat=True))
        # Всё, кроме закрытых: у готового сырьё снимает своя задача сразу после `commit`,
        # а НЕ вышедшее держим — в админке задание возвращают в очередь, поставив «ждёт»,
        # и без сырья такой повтор просто упал бы второй раз.
        keys |= set(
            MediaJob.objects.exclude(status=MediaJob.Status.DONE).values_list("source", flat=True)
        )
        return keys

    def stray_sets(self, storage, cutoff, apply):
        """Папки готовых наборов, на которые не смотрит ни лекция, ни задание.

        Имя папке даёт `intake.views.plan` и тут же записывает его в задание, поэтому
        бесхозной она становится только потеряв хозяина: удалили задание в обход сигналов
        (bulk-delete, правка базы руками) или упавший Redis не донёс уборочную задачу.
        А весит такая папка гигабайты, и найти её в бакете больше нечем — в базе не
        осталось ни строки с этим именем.

        Отдельно от `uploads/` не только по месту: там сироту опознают по отсутствию
        записи `File`, а здесь — по отсутствию сразу двух видов хозяина.
        """
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
            # Возраст берём по первому же куску: у папки в бакете времени нет вовсе,
            # а спрашивать про каждый из тысяч — тысячи запросов.
            keys = sorted(under(prefix))
            if not keys or storage.get_modified_time(keys[0]) > cutoff:
                continue
            found += 1
            self.stdout.write(f"{prefix} ({len(keys)} кусков)")
            pieces += drop_prefix(prefix) if apply else len(keys)
        return found, pieces

    def abandoned(self, storage, cutoff, apply):
        """Начатые и не собранные многочастные загрузки.

        Их части лежат в бакете и стоят денег, но объектом ещё не стали — обычным
        обходом каталога их не видно вовсе, только отдельным запросом. Копятся они
        от закрытых вкладок и оборванной связи, то есть постоянно.
        """
        client = getattr(getattr(storage, "connection", None), "meta", None)
        if client is None:
            return 0  # локальный диск: многочастных загрузок там не бывает

        client = client.client
        found, marker = 0, {}
        while True:
            answer = client.list_multipart_uploads(Bucket=storage.bucket_name, **marker)
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
