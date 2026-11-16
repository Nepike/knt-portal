# Стадия сборки CSS (Tailwind standalone).
FROM debian:bookworm-slim AS debian
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
ARG TAILWIND_VERSION=4.3.1
# Прокси curl берёт из предопределённого HTTPS_PROXY. Не `ARG PROXY`: строку RUN сборка
# печатает с подставленными значениями, и пароль прокси попал бы в лог деплоя.
RUN curl -SL --connect-timeout 30 \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-x64 \
    -o /usr/local/bin/tailwindcss && chmod +x /usr/local/bin/tailwindcss
COPY . .
RUN tailwindcss -i theme/input.css -o core/static/core/css/base.css --minify

# Главный образ. trixie явно: клиент Postgres 17 берётся из его штатных пакетов.
FROM python:3.12-slim-trixie
# glibc держит арену памяти на поток и почти не отдаёт её. Под ASGI вьюхи идут в пуле
# потоков, и воркер web разрастался. Двух арен хватает.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
WORKDIR /app

# pg_dump для ночного бэкапа (core/backup.py). Версия — как у сервера в docker-compose.yml:
# клиент младше сервера дамп не снимет.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 && rm -rf /var/lib/apt/lists/*

# Правка выше этой строки сбрасывает кэш pip, и пакеты ставятся заново. Всё, что меняется
# чаще требований, — ниже.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=debian /app/core/static/core/css/base.css core/static/core/css/base.css

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Коммит в образе (settings.SITE_VERSION). Последним слоем: значение новое на каждую выкатку.
ARG KNT_VERSION=""
ENV KNT_VERSION=${KNT_VERSION}
ENTRYPOINT ["/entrypoint.sh"]
# gunicorn — менеджер процессов, воркеры uvicorn держат сокеты. Воркеров два, по числу ядер.
# --max-requests: отслуживший воркер сменяется свежим, и память не копится; jitter — чтобы
# не оба разом. Сокеты чата и Стены переподключаются сами.
CMD ["gunicorn", "knt.asgi:application", "-k", "uvicorn_worker.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "--max-requests", "2000", "--max-requests-jitter", "200"]
