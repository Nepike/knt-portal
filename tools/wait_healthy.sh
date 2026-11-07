#!/bin/sh
# Ждёт, пока сайт ответит на /healthz/ хешем коммита, который только что выкатили
# (.github/workflows/deploy.yml). Не дождался — код 1, и деплой считается упавшим.
#
#   sh tools/wait_healthy.sh <хеш коммита> [адрес]
#
# `docker compose up -d` рапортует об успехе, даже если web тут же уходит в цикл
# перезапусков (так было со сломанным collectstatic). Хеш, а не просто «ok»: старый
# контейнер, оставшийся отвечать, тоже сказал бы «ok».
#
# Идём через nginx этой же машины (--resolve): проверяется вся цепочка, но без выхода в
# интернет. Логи web сюда не печатаем: лог Actions читает любой вошедший на GitHub.

want="$1"
url="${2:-https://knt-mipt.ru/healthz/}"
# Свежий web готов секунд через 12 (collectstatic, замер 7 ноя 2026); две минуты — с запасом
# на подкачку.
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
