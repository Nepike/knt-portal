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
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=debian /app/core/static/core/css/base.css core/static/core/css/base.css

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
# ASGI: gunicorn остаётся менеджером процессов, но воркеры uvicorn'овские — держат сокеты.
# Воркеров 2, по числу ядер: они асинхронные, каждый держит много соединений разом,
# и третий на двухъядерной машине не добавляет пропускной способности, а память ест (~75 МБ).
CMD ["gunicorn", "knt.asgi:application", "-k", "uvicorn_worker.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]
