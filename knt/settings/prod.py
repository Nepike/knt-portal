from .base import *  # noqa: F403
from .base import env

DEBUG = False
SECRET_KEY = env("SECRET_KEY")
# inbicst.ru и fnbic.ru nginx разворачивает на knt-mipt.ru; в списке — на случай, если перестанет.
ALLOWED_HOSTS = ["knt-mipt.ru", "inbicst.ru", "fnbic.ru", "files.inbicst.ru"]
# files. в доверенных источниках не нужен: на него только ходят за файлами, форм там нет.
CSRF_TRUSTED_ORIGINS = ["https://knt-mipt.ru", "https://inbicst.ru", "https://fnbic.ru"]

# Байты отдаёт nginx (см. nginx-files.conf), приложение возвращает только заголовок.
MEDIA_ACCEL = True

DATABASES = {"default": env.db("DATABASE_URL")}

# Статику раздаёт whitenoise, сразу за SecurityMiddleware. По имени: первым стоит замер запросов.
MIDDLEWARE.insert(MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,  # noqa: F405
                  "whitenoise.middleware.WhiteNoiseMiddleware")
STORAGES["staticfiles"]["BACKEND"] = "whitenoise.storage.CompressedManifestStaticFilesStorage"  # noqa: F405

EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 465
EMAIL_USE_SSL = True
EMAIL_HOST_USER = "info@knt-mipt.ru"
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")

# Без этого при DEBUG=False Django пишет ошибки только на почту ADMINS — в логи контейнера ничего.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# Redis — общая шина: сокеты клиентов живут в разных воркерах gunicorn.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": REDIS_HOSTS},  # noqa: F405
    }
}

# Тот же Redis, что и у шины чата, но другие базы (0 — шина, 1 — очередь, 2 — ответы, 3 — кэш).
CELERY_BROKER_URL = "redis://redis:6379/1"
CELERY_RESULT_BACKEND = "redis://redis:6379/2"

# Кэш — база 3, общая для всех воркеров: на ней ограничители (core/throttle.py).
# Не на базе очереди: cache.clear() — это FLUSHDB.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://redis:6379/3",
        "OPTIONS": {
            # У redis-py 6 таймаутов по умолчанию нет: подвисший Redis держал бы воркер.
            # throttled() обходится без кэша.
            "socket_connect_timeout": 2,
            "socket_timeout": 2,
        },
    }
}

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_CONTENT_TYPE_NOSNIFF = True
