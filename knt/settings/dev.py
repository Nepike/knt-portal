from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
SECRET_KEY = "django-insecure-dev-only"

DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

# Письмо идёт через очередь, печатает его воркер.
EMAIL_DELIVERY_BACKEND = "core.mail.DevConsoleBackend"
# Телеграм — так же: доезжает до задачи и печатается.
TELEGRAM_CONSOLE = True

# Слой каналов в памяти: события не переходят между процессами (у manage.py shell свой).
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Очереди нужен Redis — контейнером (README). Базы: 0 — шина, 1 — очередь, 2 — ответы, 3 — кэш (прод).
CELERY_BROKER_URL = "redis://127.0.0.1:6379/1"
CELERY_RESULT_BACKEND = "redis://127.0.0.1:6379/2"

SITE_THEME = "glitch"  # ВРЕМЕННО: смотрим 2 декабря до срока, не коммитить
