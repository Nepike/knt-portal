#!/bin/sh
# Весь набор тестов в боевом образе. Раз в месяц — из .github/workflows/selftest.yml;
# руками на сервере:
#
#   cd /srv/knt && sh tools/selftest.sh [метки тестов, как у manage.py test]
#
# Ловит то, чем сервер разошёлся с разработкой: Linux, пакеты образа, postgres:17.
# От прода отгорожен: своя одноразовая база и сеть, без .env, настройки dev, потолки памяти и CPU.

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

# По TCP, не через сокет: при первом старте образ поднимает временный сервер только на сокете.
i=0
until docker exec "$db" pg_isready -q -h 127.0.0.1 -U knt; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "База для тестов не поднялась за минуту" >&2
    exit 3
  fi
  sleep 1
done

# Полгига памяти, одно ядро из двух, низкий приоритет: сайту рядом должно хватать.
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
