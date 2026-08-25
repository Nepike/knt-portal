"""Обложка-кадр больше не нужна: списки показывают живую анимацию.

Блобы приходится сносить руками. Сигнал `attachments.storage` чистит хранилище при
удалении ЗАПИСИ, а тут пропадает только поле — без этого шага 49 файлов остались бы
в бакете навсегда, и добраться до них было бы уже неоткуда.
"""

from django.db import migrations


def drop_blobs(apps, schema_editor):
    CosmeticItem = apps.get_model("cosmetics", "CosmeticItem")
    for item in CosmeticItem.objects.exclude(still="").iterator():
        item.still.delete(save=False)


def keep_blobs(apps, schema_editor):
    """Назад файлы не вернуть — но и мешать откату схемы незачем."""


class Migration(migrations.Migration):

    dependencies = [
        ("cosmetics", "0004_cosmeticitem_price_cosmeticitem_sold"),
    ]

    operations = [
        migrations.RunPython(drop_blobs, keep_blobs),
        migrations.RemoveField(
            model_name="cosmeticitem",
            name="still",
        ),
    ]
