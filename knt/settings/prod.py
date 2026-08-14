from .base import *  # noqa: F403
from .base import env

DEBUG = False
SECRET_KEY = env("SECRET_KEY")
# inbicst.ru и fnbic.ru nginx разворачивает на knt-mipt.ru, до приложения они не доходят —
# держим их в списке на случай, если однажды перестанет.
ALLOWED_HOSTS = ["knt-mipt.ru", "inbicst.ru", "fnbic.ru", "files.inbicst.ru"]
# files. в доверенных источниках не нужен: на него только ходят за файлами, форм там нет.
CSRF_TRUSTED_ORIGINS = ["https://knt-mipt.ru", "https://inbicst.ru", "https://fnbic.ru"]

# Байты отдаёт nginx (см. nginx-files.conf), приложение возвращает только заголовок.
MEDIA_ACCEL = True

DATABASES = {"default": env.db("DATABASE_URL")}

# Статику раздаёт сам Django через whitenoise (nginx только проксирует + TLS).
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")  # noqa: F405
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

# Воркеров у gunicorn несколько, и сокет клиента живёт в одном из них — Redis тут общая шина:
# отправитель публикует в своём процессе, получатели слушают в своих.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": REDIS_HOSTS},  # noqa: F405
    }
}

# Тот же Redis, что и у шины чата, но другие базы (0 — шина, 1 — очередь, 2 — ответы, 3 — кэш).
CELERY_BROKER_URL = "redis://redis:6379/1"
CELERY_RESULT_BACKEND = "redis://redis:6379/2"

# База 3 — кэш. На нём живут ограничители частоты (core/throttle.py): поддержка и
# восстановление пароля считают обращения с адреса. В памяти процесса этот счётчик
# был у КАЖДОГО воркера свой, то есть лимит на деле был вдвое выше объявленного
# и обнулялся любым перезапуском. Своя база, а не общая с очередью: `cache.clear()`
# у этого бекенда — это FLUSHDB, и на общей базе он унёс бы ещё и задачи Celery.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://redis:6379/3",
        "OPTIONS": {
            # Явные таймауты: у redis-py 6 по умолчанию их нет вовсе, и подвисший Redis
            # держал бы воркер бесконечно. Кэш — не то, ради чего стоит ждать: две
            # секунды и мимо (throttled() умеет обходиться без него).
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
