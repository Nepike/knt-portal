# Новый (опять) сайт факультета!

```.\tailwindcss.exe -i theme/input.css -o core/static/core/css/base.css --watch```
docker compose exec web python manage.py clean_uploads --days 1 --apply

## Фоновые задачи (Celery)

Очереди нужен Redis. Локально — контейнером:

```docker run -d --name knt-redis -p 127.0.0.1:6379:6379 redis:8-alpine redis-server --save "" --appendonly no```

Воркер. На Windows обязателен `--pool=solo`: обычный prefork форкает процесс, а Windows этого не умеет.

```.\.venv\Scripts\celery.exe -A knt worker -l info --pool=solo```

Проверка всей связки «сайт → Redis → воркер»:

```python manage.py celery_check```

В проде воркер поднимается сервисом `worker` из docker-compose, Redis там уже есть.
Базы Redis разведены: 0 — шина чата, 1 — очередь задач, 2 — ответы задач.

Письма тоже идут очередью, поэтому в разработке они печатаются **в окне воркера**, а не
сервера. Если воркер не запущен, письмо просто ждёт в очереди; если лежит сам Redis —
уходит напрямую, без очереди.
