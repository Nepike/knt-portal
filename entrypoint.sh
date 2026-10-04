#!/bin/sh
set -e
# Миграции здесь не катятся: их до старта всех сервисов делает migrate (docker-compose.yml).
python manage.py collectstatic --noinput
exec "$@"
