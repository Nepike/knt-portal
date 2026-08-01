# --- Сборка CSS: standalone-бинарь Tailwind под Linux ---
FROM debian:bookworm-slim AS css
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Версия совпадает с локальным tailwindcss.exe — иначе CSS может разойтись.
# PROXY: с сервера release-assets.githubusercontent.com заблокирован — качаем
# через прокси (значение только в серверном .env, в git не попадает).
ARG TAILWIND_VERSION=4.3.1
ARG PROXY
RUN curl -sSL --connect-timeout 30 ${PROXY:+-x $PROXY} \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-x64 \
    -o /usr/local/bin/tailwindcss && chmod +x /usr/local/bin/tailwindcss
COPY . .
RUN tailwindcss -i theme/input.css -o core/static/core/css/base.css --minify

# --- Приложение ---
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=css /app/core/static/core/css/base.css core/static/core/css/base.css

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "knt.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
