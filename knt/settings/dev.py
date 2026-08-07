from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
SECRET_KEY = "django-insecure-dev-only"

DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

# Письмо всё так же уходит в очередь — печатает его воркер, в своём окне.
EMAIL_DELIVERY_BACKEND = "core.mail.DevConsoleBackend"
# И телеграм тем же порядком: сообщение доезжает до задачи, но печатается, а не улетает в чат.
TELEGRAM_CONSOLE = True

# runserver — один процесс, ему хватает слоя в памяти: Redis локально не нужен.
# Обратная сторона: события НЕ переходят между процессами (у manage.py shell свой слой).
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Очереди Redis всё-таки нужен — поднимается контейнером (см. README). Базы разные:
# 0 — шина чата, 1 — очередь задач, 2 — ответы; flushdb в одной не сносит остальные.
CELERY_BROKER_URL = "redis://127.0.0.1:6379/1"
CELERY_RESULT_BACKEND = "redis://127.0.0.1:6379/2"
