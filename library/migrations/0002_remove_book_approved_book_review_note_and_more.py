# Порядок операций переставлен вручную: автогенератор снёс бы approved ДО того,
# как из него перенесли данные, и все опубликованные книги вернулись бы на проверку.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def approved_to_status(apps, schema_editor):
    Book = apps.get_model("library", "Book")
    Book.objects.filter(approved=True).update(status="approved")


def status_to_approved(apps, schema_editor):
    Book = apps.get_model("library", "Book")
    Book.objects.filter(status="approved").update(approved=True)


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='book',
            name='review_note',
            field=models.CharField(blank=True, max_length=300, verbose_name='причина отказа'),
        ),
        migrations.AddField(
            model_name='book',
            name='reviewed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='проверено'),
        ),
        migrations.AddField(
            model_name='book',
            name='reviewed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='проверил'),
        ),
        migrations.AddField(
            model_name='book',
            name='status',
            field=models.CharField(choices=[('pending', 'на проверке'), ('approved', 'опубликовано'), ('rejected', 'отклонено')], db_index=True, default='pending', max_length=10, verbose_name='статус'),
        ),
        migrations.RunPython(approved_to_status, status_to_approved),
        migrations.RemoveField(
            model_name='book',
            name='approved',
        ),
    ]
