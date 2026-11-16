#!/bin/sh
# Ждёт, что сайт ответит на /healthz/ хешем только что выкаченного коммита; не дождался — код 1.
#
#   sh tools/wait_healthy.sh <хеш коммита> [адрес]
#
# Хеш, а не «ok»: старый контейнер тоже ответил бы «ok». Идём через nginx этой машины (--resolve).
# Логи web не печатаем: лог Actions читает любой вошедший на GitHub.

want="$1"
url="${2:-https://knt-mipt.ru/healthz/}"
# Свежий web готов секунд через 12; две минуты — с запасом.
tries=40
pause=3

if [ -z "$want" ]; then
  echo "Не сказано, какой версии ждать" >&2
  exit 2
fi

i=0
while [ "$i" -lt "$tries" ]; do
  got=$(curl -fsS --max-time 5 --resolve knt-mipt.ru:443:127.0.0.1 "$url" 2>&1)
  if [ "$got" = "ok $want" ]; then
    echo "Сайт отвечает: $got"
    exit 0
  fi
  i=$((i + 1))
  sleep "$pause"
done

echo "За $((tries * pause)) с сайт не ответил версией $want. Последний ответ: ${got:-пусто}" >&2
echo "Смотреть на сервере: cd /srv/knt && docker compose ps && docker compose logs --tail 100 web" >&2
exit 1
