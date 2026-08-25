"""Пересобирает пофайловые строки «скачивают» в строки-порции по 50 токенов.

Награда за скачивания стала копиться и выплачиваться порциями (`rewards._downloads`),
и ключ у неё теперь — номер порции. Без этой миграции прежние строки с ключами
`download|<номер файла>` не зачлись бы против новых `download|1`, `download|2`… —
и `sync` заплатил бы всем повторно, всю сумму целиком.

Деньги не меняются ни на токен: сумма по кошельку та же, просто разложена по порциям.
Хвост меньше порции кладём в последнюю строку как есть — он уже выплачен, а забирать
выплаченное нельзя; следующий пересчёт просто допишет до полной полусотни.

Строки НЕ создаются заново, а переписываются поверх старых: порядок в журнале идёт по
номеру строки, и новые уехали бы в конец ленты, притворившись сегодняшними. Мест всегда
хватает — каждая старая строка это максимум потолок на файл (50), то есть ровно порция.

`balance_after` пересчитывается по всему журналу: строки исчезают из середины ленты,
и без пересчёта у выживших остались бы цифры от прежнего порядка.
"""

from collections import defaultdict

from django.db import migrations

DOWNLOAD = "download"
BATCH = 50
NOTE = "скачивают твои файлы"


def _split(total, places):
    """Разложить сумму по порциям. Больше `places` кусков не делаем: не во что писать,
    и тогда лишнее уходит в последний. На боевых данных этого не случается."""
    parts = []
    left = total
    while left > 0 and len(parts) < places:
        parts.append(min(left, BATCH))
        left -= BATCH
    if left > 0:
        parts[-1] += left
    return parts


def regroup(apps, schema_editor):
    BalanceLog = apps.get_model("economy", "BalanceLog")

    groups = defaultdict(list)
    for row in BalanceLog.objects.filter(reason=DOWNLOAD).order_by("id").values("id", "wallet_id", "amount"):
        groups[row["wallet_id"]].append(row)

    doomed = []
    for rows in groups.values():
        parts = _split(sum(row["amount"] for row in rows), len(rows))
        for number, (row, amount) in enumerate(zip(rows, parts), start=1):
            BalanceLog.objects.filter(id=row["id"]).update(key=str(number), note=NOTE, amount=amount)
        doomed += [row["id"] for row in rows[len(parts):]]

    # Пачками: список бывает в тысячи номеров, а IN на всю тысячу некоторые базы не любят.
    for start in range(0, len(doomed), 500):
        BalanceLog.objects.filter(id__in=doomed[start : start + 500]).delete()

    _restate(BalanceLog)


def _restate(BalanceLog):
    """Пересчитать «баланс после» по всему журналу, кошелёк за кошельком."""
    running = defaultdict(int)
    fixed = []
    for row in BalanceLog.objects.order_by("id").values("id", "wallet_id", "amount", "balance_after"):
        running[row["wallet_id"]] += row["amount"]
        if running[row["wallet_id"]] != row["balance_after"]:
            fixed.append(BalanceLog(id=row["id"], balance_after=running[row["wallet_id"]]))
    if fixed:
        BalanceLog.objects.bulk_update(fixed, ["balance_after"], batch_size=500)


def back(apps, schema_editor):
    """Разобрать порции обратно нельзя: из суммы не восстановить, какие файлы её
    составили. Ставим no-op, чтобы миграция откатывалась хотя бы формально — суммы
    и балансы при откате остаются верными, теряется только пофайловая разбивка."""


class Migration(migrations.Migration):
    dependencies = [("economy", "0005_alter_balancelog_reason")]

    operations = [migrations.RunPython(regroup, back)]
