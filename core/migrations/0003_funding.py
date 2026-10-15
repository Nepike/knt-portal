import core.models
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_lockdown'),
    ]

    operations = [
        migrations.CreateModel(
            name='Funding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('since', models.DateField(default=core.models.this_month, help_text='Любое число месяца: считается весь месяц.', verbose_name='с месяца')),
                ('until', models.DateField(help_text='Любое число месяца: считается весь месяц.', verbose_name='по месяц включительно')),
                ('skip', models.TextField(blank=True, help_text='Имена как в выгрузке, по одному в строке: тестовые платежи и те, кто просил не упоминать.', verbose_name='не показывать')),
                ('loaded', models.DateField(blank=True, editable=False, null=True, verbose_name='выгрузка загружена')),
            ],
            options={
                'verbose_name': 'сбор',
                'verbose_name_plural': 'сборы',
                'ordering': ['-until'],
            },
        ),
        migrations.CreateModel(
            name='Supporter',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='Настоящее имя полностью — только с согласия человека.', max_length=60, verbose_name='подпись')),
                ('funding', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='supporters', to='core.funding', verbose_name='сбор')),
            ],
            options={
                'verbose_name': 'спонсор',
                'verbose_name_plural': 'спонсоры',
            },
        ),
    ]
