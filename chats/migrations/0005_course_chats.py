# Чат учебной группы → чат курса: один поток вместо десятка мелких чатов.
# Адресация парой «год поступления + ступень» — номер курса растёт каждый сентябрь, год нет.
from django.db import migrations, models
from django.db.models import Q

STAGES = {"bachelor": "Бакалавриат", "master": "Магистратура"}


def to_course_chats(apps, schema_editor):
    """Переезд без потерь: сообщения групп одного потока сливаются в один чат
    (id сохраняются, поэтому порядок ленты и отметки прочитанного остаются валидными)."""
    Chat = apps.get_model("chats", "Chat")
    Membership = apps.get_model("chats", "Membership")
    Message = apps.get_model("chats", "Message")

    for chat in Chat.objects.filter(kind="team").select_related("team").order_by("id"):
        if chat.team is None:
            chat.delete()
            continue
        year, stage = chat.team.year_of_admission, chat.team.stage
        target = Chat.objects.filter(kind="course", admission_year=year, stage=stage).first()
        if target is None:
            chat.kind = "course"
            chat.admission_year = year
            chat.stage = stage
            chat.title = f"{STAGES.get(stage, stage)} {year}"
            chat.save(update_fields=["kind", "admission_year", "stage", "title"])
            continue

        Message.objects.filter(chat=chat).update(chat=target)
        already = set(Membership.objects.filter(chat=target).values_list("user_id", flat=True))
        Membership.objects.filter(chat=chat).exclude(user_id__in=already).update(chat=target)
        chat.last_message = None
        chat.save(update_fields=["last_message"])
        chat.delete()  # оставшиеся memberships — дубли, уйдут каскадом

    for chat in Chat.objects.filter(kind="course"):
        last = Message.objects.filter(chat=chat).order_by("-id").first()
        if chat.last_message_id != (last.pk if last else None):
            chat.last_message = last
            chat.save(update_fields=["last_message"])


def rename_permission(apps, schema_editor):
    """Переименовываем, а не пересоздаём: pk сохраняется — значит группа «Кураторы»
    и выданные вручную права остаются на месте."""
    apps.get_model("auth", "Permission").objects.filter(codename="curate_team_chats").update(
        codename="curate_course_chats", name="Может быть куратором чата курса",
    )


def restore_permission(apps, schema_editor):
    apps.get_model("auth", "Permission").objects.filter(codename="curate_course_chats").update(
        codename="curate_team_chats", name="Может быть куратором чата учебной группы",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("chats", "0004_alter_message_updated"),
        ("auth", "__first__"),
    ]

    operations = [
        migrations.AddField(
            model_name="chat",
            name="admission_year",
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="год поступления"),
        ),
        migrations.AddField(
            model_name="chat",
            name="stage",
            field=models.CharField(
                blank=True, max_length=20, verbose_name="ступень обучения",
                choices=[("bachelor", "Бакалавриат"), ("master", "Магистратура")],
            ),
        ),
        migrations.AlterField(
            model_name="chat",
            name="kind",
            field=models.CharField(
                default="dm", max_length=10, verbose_name="тип",
                choices=[("dm", "личный"), ("group", "групповой"), ("course", "курс")],
            ),
        ),
        migrations.AlterModelOptions(
            name="chat",
            options={
                "ordering": ["-id"],
                "permissions": [("curate_course_chats", "Может быть куратором чата курса")],
                "verbose_name": "чат",
                "verbose_name_plural": "чаты",
            },
        ),
        # Данные переносим, пока поле team ещё на месте.
        migrations.RunPython(to_course_chats, migrations.RunPython.noop),
        migrations.RemoveField(model_name="chat", name="team"),
        migrations.AddConstraint(
            model_name="chat",
            constraint=models.UniqueConstraint(
                condition=Q(kind="course"), fields=("admission_year", "stage"), name="unique_course_chat",
            ),
        ),
        migrations.RunPython(rename_permission, restore_permission),
    ]
