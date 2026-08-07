# Порядок операций переставлен вручную — см. library/migrations/0002: approved
# нужно снести ПОСЛЕ переноса данных, иначе всё опубликованное вернётся на проверку.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def approved_to_status(apps, schema_editor):
    Material = apps.get_model("materials", "Material")
    Material.objects.filter(approved=True).update(status="approved")


def status_to_approved(apps, schema_editor):
    Material = apps.get_model("materials", "Material")
    Material.objects.filter(status="approved").update(approved=True)


class Migration(migrations.Migration):

    dependencies = [
        ('materials', '0002_alter_comment_options_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='material',
            name='review_note',
            field=models.CharField(blank=True, max_length=300, verbose_name='причина отказа'),
        ),
        migrations.AddField(
            model_name='material',
            name='reviewed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='проверено'),
        ),
        migrations.AddField(
            model_name='material',
            name='reviewed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='проверил'),
        ),
        migrations.AddField(
            model_name='material',
            name='status',
            field=models.CharField(choices=[('pending', 'на проверке'), ('approved', 'опубликовано'), ('rejected', 'отклонено')], db_index=True, default='pending', max_length=10, verbose_name='статус'),
        ),
        migrations.RunPython(approved_to_status, status_to_approved),
        migrations.RemoveField(
            model_name='material',
            name='approved',
        ),
    ]
