# Роль «Кураторы»: группа в БД с правом chats.curate_course_chats.
# Право создаём сами: post_migrate генерирует permissions только ПОСЛЕ всех
# миграций, на свежей базе его бы ещё не было.
from django.db import migrations


def create_curators(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    content_type, _ = ContentType.objects.get_or_create(app_label="chats", model="chat")
    permission, _ = Permission.objects.get_or_create(
        codename="curate_course_chats",
        content_type=content_type,
        defaults={"name": "Может быть куратором чата курса"},
    )
    group, _ = Group.objects.get_or_create(name="Кураторы")
    group.permissions.add(permission)


def drop_curators(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name="Кураторы").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("chats", "0002_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(create_curators, drop_curators),
    ]
