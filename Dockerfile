# Собираем ccs в контейнере с Debain
FROM debian:bookworm-slim AS debian
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
ARG TAILWIND_VERSION=4.3.1
ARG PROXY
RUN curl -SL --connect-timeout 30 ${PROXY:+-x $PROXY} \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-x64 \
    -o /usr/local/bin/tailwindcss && chmod +x /usr/local/bin/tailwindcss
COPY . .
RUN tailwindcss -i theme/input.css -o core/static/core/css/base.css --minify

# Главный контейнер с Django
# trixie назван явно: клиент Postgres 17 ниже берётся из его штатных пакетов, и молчаливая
# смена выпуска Debian под тем же тегом однажды оставила бы сборку без этого пакета.
FROM python:3.12-slim-trixie
# MALLOC_ARENA_MAX: glibc заводит память отдельно под каждый поток и почти не отдаёт её
# назад. Синхронные вьюхи под ASGI идут в пуле потоков, и воркер web за три дня дорастал
# до ~580 МБ (замер 7 ноя 2026) на машине с 1,9 ГБ. Двух арен процессу хватает.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
WORKDIR /app

# pg_dump для ночного бэкапа базы (core/backup.py). Версия — та же, что у сервера
# в docker-compose.yml: клиент младше сервера снять дамп откажется.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 && rm -rf /var/lib/apt/lists/*

# Всё выше этой строки сбрасывает кэш pip: правка любой из них — и пакеты ставятся заново,
# а это минуты на медленном канале (7 ноя 2026 сборка из-за этого не влезла в деплой).
# Новое, что меняется чаще требований, — ниже, после установки пакетов.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=debian /app/core/static/core/css/base.css core/static/core/css/base.css

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Какой коммит в образе — для «Системы» Пульса (settings.SITE_VERSION). Последним слоем:
# значение новое на каждую выкатку и сбросило бы кэш всех слоёв ниже.
ARG KNT_VERSION=""
ENV KNT_VERSION=${KNT_VERSION}
ENTRYPOINT ["/entrypoint.sh"]
# ASGI: gunicorn остаётся менеджером процессов, но воркеры uvicorn'овские — держат сокеты.
# Воркеров 2, по числу ядер: они асинхронные, каждый держит много соединений разом,
# и третий на двухъядерной машине не добавляет пропускной способности, а память ест (~75 МБ).
# --max-requests: отслужив порцию, воркер уходит, и gunicorn ставит свежий — память не
# копится неделями. Порцию берут со случайной добавкой, чтобы оба не ушли разом. Сокеты
# чата и Стены при этом переподключаются сами, чат догоняет пропущенное по курсору.
CMD ["gunicorn", "knt.asgi:application", "-k", "uvicorn_worker.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "--max-requests", "2000", "--max-requests-jitter", "200"]
