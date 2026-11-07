#!/bin/sh
# Весь набор тестов внутри боевого образа. Раз в месяц его запускает
# .github/workflows/selftest.yml и шлёт итог в телеграм; руками — на сервере:
#
#   cd /srv/knt && sh tools/selftest.sh [метки тестов, как у manage.py test]
#
# Перед пушем тесты гоняются на Windows и на своём Postgres. Здесь тот же код идёт там, где он
# работает: Linux, пакеты из образа, тот же postgres:17. Разъедется сервер с разработкой —
# всплывёт здесь.
#
# От прода прогон отгорожен: своя одноразовая база в своей сети, файла окружения сервера нет.
# Боевой базы, бакета, почты и телеграма тесты не видят — ни адресов, ни ключей. Настройки —
# dev, как у разработчика. Потолки памяти и процессора — чтобы сайт рядом не встал.

set -u
image=knt-app
net=knt-selftest
db=knt-selftest-db
run=knt-selftest-run

cleanup() {
  docker rm -fv "$run" "$db" >/dev/null 2>&1
  docker network rm "$net" >/dev/null 2>&1
}
cleanup  # хвосты прошлого прогона, если его оборвали
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

version=$(docker image inspect "$image" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
  | sed -n 's/^KNT_VERSION=//p')
echo "Образ: ${version:-неизвестно}"

docker network create "$net" >/dev/null || exit 3
# fsync и прочее выключены: базу выбросят через минуты, а тесты пишут много.
docker run -d --name "$db" --network "$net" --memory 256m \
  -e POSTGRES_USER=knt -e POSTGRES_PASSWORD=knt -e POSTGRES_DB=knt \
  postgres:17 -c shared_buffers=32MB -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
  >/dev/null || exit 3

# По TCP, а не через сокет: при первом старте образ поднимает временный сервер только на сокете,
# а потом перезапускает его — проверка через сокет прошла бы раньше времени.
i=0
until docker exec "$db" pg_isready -q -h 127.0.0.1 -U knt; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "База для тестов не поднялась за минуту" >&2
    exit 3
  fi
  sleep 1
done

# Полгига своей памяти и дальше — в подкачку свою, а не соседей; одно ядро из двух и низкий
# приоритет на нём. Сайт ночью почти пуст, но ему должно хватать и в это время.
start=$(date +%s)
docker run --rm --name "$run" --network "$net" \
  --memory 512m --memory-swap 1536m --cpus 1 --cpu-shares 256 \
  -e DJANGO_SETTINGS_MODULE=knt.settings.dev \
  -e DATABASE_URL="postgres://knt:knt@$db:5432/knt" \
  -e R2_BUCKET= \
  --entrypoint python "$image" manage.py test --noinput "$@" 2>&1
code=$?

echo "Шло $(( ($(date +%s) - start) / 60 )) мин, код выхода $code"
if [ "$code" -eq 137 ]; then
  echo "Процесс тестов убит — похоже, не хватило памяти"
fi
exit "$code"
