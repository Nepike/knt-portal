"""Переносит обсуждения материалов в новое приложение.

Копируем, а не переименовываем таблицу: переименование не тронуло бы таблицы лайков
(их имена собираются из имени таблицы модели), и получилась бы половина переезда.
Копия видна целиком и проверяется тестом.

**Номера сохраняем.** На них ссылается `parent` внутри самой ленты, и на них же стоят
ключи уже выплаченных наград за лайки (`economy.rewards`, ключ `c<номер>`): сменись
номер — и за старый комментарий заплатили бы второй раз.
"""

from django.core.management.color import no_style
from django.db import migrations


def carry(apps, schema_editor):
    old_comments = apps.get_model("materials", "Comment")
    new_comments = apps.get_model("comments", "Comment")

    rows = list(old_comments.objects.all())
    if not rows:
        return  # чистая база: переносить нечего

    # Двумя проходами: `parent` показывает на такой же комментарий, которого на момент
    # вставки может ещё не быть.
    new_comments.objects.bulk_create([
        new_comments(
            id=row.id, material_id=row.material_id, author_id=row.author_id,
            hide_author=row.hide_author, text=row.text, image=row.image.name,
            created=row.created,
        )
        for row in rows
    ])
    for row in rows:
        if row.parent_id:
            new_comments.objects.filter(id=row.id).update(parent_id=row.parent_id)

    # Голоса копируем прямо строками связующей таблицы: номера комментариев сохранены,
    # значит это ровно тот же набор пар «комментарий — человек».
    for field in ("liked_users", "disliked_users"):
        was = getattr(old_comments, field).through
        became = getattr(new_comments, field).through
        became.objects.bulk_create([
            became(comment_id=comment, user_id=user)
            for comment, user in was.objects.values_list("comment_id", "user_id")
        ])

    # Номера проставлены руками, а счётчик таблицы об этом не знает — следующая вставка
    # выдала бы занятый номер. На SQLite список пустой: там счётчиков нет.
    for sql in schema_editor.connection.ops.sequence_reset_sql(no_style(), [new_comments]):
        schema_editor.execute(sql)


def carry_permission(apps, schema_editor):
    """Право «править чужие комментарии» — за моделью следом.

    У нового приложения свой app_label, поэтому выданное на `materials.change_comment`
    перестаёт что-либо значить. Пропало бы оно молча: модератор просто обнаружил бы,
    что чужой мусор больше не убирается.

    Само право заводит `post_migrate` уже после всех миграций, поэтому строку создаём
    здесь сами — потом она просто найдётся готовой.
    """
    content_types = apps.get_model("contenttypes", "ContentType")
    permissions = apps.get_model("auth", "Permission")
    groups = apps.get_model("auth", "Group")
    users = apps.get_model("users", "User")

    was = content_types.objects.filter(app_label="materials", model="comment").first()
    if was is None:
        return
    became, _ = content_types.objects.get_or_create(app_label="comments", model="comment")

    for right in permissions.objects.filter(content_type=was):
        moved, _ = permissions.objects.get_or_create(
            content_type=became, codename=right.codename, defaults={"name": right.name},
        )
        for group in groups.objects.filter(permissions=right):
            group.permissions.add(moved)
        for user in users.objects.filter(user_permissions=right):
            user.user_permissions.add(moved)


class Migration(migrations.Migration):

    dependencies = [
        ("comments", "0001_initial"),
        ("materials", "0003_free_the_comments_name"),
        ("auth", "0001_initial"),
        ("contenttypes", "0001_initial"),
    ]

    # Назад не отыгрываем: следом идёт удаление старой таблицы, и «откатить перенос»
    # означало бы вернуть данные туда, где их уже нет. Откат — из резервной копии.
    operations = [
        migrations.RunPython(carry, migrations.RunPython.noop),
        migrations.RunPython(carry_permission, migrations.RunPython.noop),
    ]
