from django.db import migrations, models
from django.db.models import F


def count_past_tries(apps, schema_editor):
    """Выдачи подряд до сих пор не считались, а номер попытки считался — берём его.
    Задание, которое 30.09.2026 заперло очередь, закроется на первом же `claim`,
    а не будет крутиться ещё MAX_TRIES раз."""
    MediaJob = apps.get_model("intake", "MediaJob")
    MediaJob.objects.update(tries=F("attempts"))


class Migration(migrations.Migration):

    dependencies = [
        ('intake', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='mediajob',
            name='attempts',
            field=models.PositiveIntegerField(default=0, verbose_name='попыток'),
        ),
        migrations.AddField(
            model_name='mediajob',
            name='tries',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='выдач подряд'),
        ),
        migrations.RunPython(count_past_tries, migrations.RunPython.noop),
    ]
