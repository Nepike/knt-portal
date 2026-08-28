"""Освобождает имя обратной связи `material.comments` перед переездом обсуждения.

Модель комментария уезжает в приложение `comments`, и там она забирает себе то же самое
`related_name`. Пока обе живы (а между созданием новой и удалением старой лежит перенос
данных), имя должно принадлежать кому-то одному — иначе `material.comments` в этот
промежуток означает неизвестно что.

Схемы это не касается: `related_name` живёт только в питоне, в базе от него ничего нет.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("materials", "0002_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="comment",
            name="material",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="legacy_comments",
                to="materials.material",
                verbose_name="материал",
            ),
        ),
    ]
