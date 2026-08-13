import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Связи File/Image. Отдельно от 0001: модели, на которые они ссылаются,
    сами ссылаются на attachments — таблицы должны появиться раньше внешних ключей."""

    initial = True

    dependencies = [
        ('attachments', '0001_initial'),
        ('library', '0001_initial'),
        ('materials', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='file',
            name='book',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='files', to='library.book', verbose_name='книга'),
        ),
        migrations.AddField(
            model_name='file',
            name='material',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='files', to='materials.material', verbose_name='материал'),
        ),
        migrations.AddField(
            model_name='file',
            name='uploader',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='uploaded_files', to=settings.AUTH_USER_MODEL, verbose_name='загрузил'),
        ),
        migrations.AddField(
            model_name='image',
            name='material',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='images', to='materials.material', verbose_name='материал'),
        ),
        migrations.AddField(
            model_name='image',
            name='uploader',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='uploaded_images', to=settings.AUTH_USER_MODEL, verbose_name='загрузил'),
        ),
    ]
