from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
SECRET_KEY = "django-insecure-dev-only"

DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

EMAIL_BACKEND = "core.mail.DevConsoleBackend"

# runserver — один процесс, ему хватает слоя в памяти: Redis локально не нужен.
# Обратная сторона: события НЕ переходят между процессами (у manage.py shell свой слой).
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
