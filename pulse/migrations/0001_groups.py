from django.db import migrations

# (группа, приложение, модель, где объявлено право, codename, подпись права)
GROUPS = [
    ("Пульс", "core", "failure", "view_pulse", "Открывает Пульс — раздел для админов"),
    ("Печать без лимита", "printing", "printjob", "unlimited", "печатать без недельного лимита"),
    ("Правка Стены", "wall", "board", "moderate_wall", "Может править Стену"),
]


def add_groups(apps, schema_editor):
    """Права в карточке человека переключаются группами — у двух последних групп не было.

    Право заводим сами, не дожидаясь post_migrate (как в core/0002_lockdown): тот
    срабатывает после ВСЕХ миграций, а группа нужна уже здесь.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")

    for name, app_label, model, codename, label in GROUPS:
        kind, _ = ContentType.objects.get_or_create(app_label=app_label, model=model)
        permission, _ = Permission.objects.get_or_create(
            content_type=kind, codename=codename, defaults={"name": label},
        )
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.add(permission)


def drop_groups(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=[name for name, *_ in GROUPS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_alter_failure_options"),
        ("printing", "0004_print_for_all"),
        ("wall", "0005_remove_wallprofile_color_remove_wallprofile_rerolls_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(add_groups, drop_groups)]
