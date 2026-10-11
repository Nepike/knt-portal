from django.db import migrations, models

GROUP = "Доступ при закрытии"


def add_group(apps, schema_editor):
    """Белый список — группа с одним правом: человека добавляют в неё в админке.

    Право заводим сами, не дожидаясь post_migrate: тот срабатывает после ВСЕХ миграций,
    а группа нужна уже здесь. Свою запись он потом найдёт и второй не создаст.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")

    kind, _ = ContentType.objects.get_or_create(app_label="core", model="lockdown")
    permission, _ = Permission.objects.get_or_create(
        content_type=kind, codename="pass_lockdown", defaults={"name": "Проходит на закрытый сайт"},
    )
    group, _ = Group.objects.get_or_create(name=GROUP)
    group.permissions.add(permission)


def drop_group(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name=GROUP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='Lockdown',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('active', models.BooleanField(default=False, verbose_name='сайт закрыт')),
                ('text', models.TextField(blank=True, help_text='Markdown. Пусто — стоит дежурная фраза.', verbose_name='текст на странице')),
                ('due', models.CharField(blank=True, choices=[('hosting', 'хостинг'), ('storage', 'файловое хранилище'), ('domains', 'домены')], help_text='Подпись к сроку. Пусто — срок стоит без неё.', max_length=10, verbose_name='что не оплачено')),
                ('deadline', models.DateField(blank=True, help_text='Пусто — плашки со сроком на странице нет.', null=True, verbose_name='крайний срок оплаты')),
                ('contact', models.CharField(blank=True, help_text='Например: Иванов Иван, ivanov@phystech.edu. Почта станет ссылкой. Пусто — строки нет.', max_length=200, verbose_name='кому писать')),
            ],
            options={
                'verbose_name': 'закрытие сайта',
                'verbose_name_plural': 'закрытие сайта',
                'permissions': [('pass_lockdown', 'Проходит на закрытый сайт')],
            },
        ),
        migrations.RunPython(add_group, drop_group),
    ]
