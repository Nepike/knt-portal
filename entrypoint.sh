#!/bin/sh
set -e
# Миграции катит сервис migrate (docker-compose.yml).
python manage.py collectstatic --noinput
exec "$@"
