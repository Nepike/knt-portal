from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")


DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
THIRD_PARTY_APPS = ["channels"]
LOCAL_APPS = [
    "core",
    "users",
    "chats",
    "teachers",
    "materials",
    "library",
    "attachments",
]
# daphne первым во всём списке — иначе не перехватит runserver и в разработке не будет WebSocket
INSTALLED_APPS = ["daphne"] + DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.HtmxRedirectMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Сайт закрытый: всё требует логина, кроме auth-страниц и явных @login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "users.middleware.MustChangePasswordMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "knt.urls"
WSGI_APPLICATION = "knt.wsgi.application"
ASGI_APPLICATION = "knt.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site_theme",
                "chats.context_processors.unread_messages",
            ],
        },
    },
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Прод переопределяет staticfiles на whitenoise (см. prod.py).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Cloudflare R2 для файлов книг и материалов (attachments.storage.media_storage).
# Пусто — работаем с локальным диском, так живёт разработка.
R2_BUCKET = env("R2_BUCKET", default="")
R2_OPTIONS = {
    "bucket_name": R2_BUCKET,
    "endpoint_url": f"https://{env('R2_ACCOUNT_ID', default='')}.r2.cloudflarestorage.com",
    "access_key": env("R2_ACCESS_KEY_ID", default=""),
    "secret_key": env("R2_SECRET_ACCESS_KEY", default=""),
    # Префикс ключей: разработка и прод могут жить в одном бакете, не перемешиваясь.
    "location": env("R2_PREFIX", default=""),
    "region_name": "auto",  # у R2 один регион, размещение задаётся при создании бакета
    "signature_version": "s3v4",
    "addressing_style": "path",  # bucket в пути: у R2 нет DNS-имён на бакет
    # Бакет закрытый, ссылку подписываем на час: сайт закрытый, файлы наружу не раздаём.
    # Скачивание всё равно идёт через attachments.views.download — там и права, и счётчик.
    "querystring_auth": True,
    "querystring_expire": 3600,
    "file_overwrite": False,  # S3-хранилище иначе молча затирает одноимённый файл
}

TEST_RUNNER = "core.test_runner.MediaIsolatedRunner"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "users.User"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/demo/"  # TODO: сменить на главную, когда появится
LOGOUT_REDIRECT_URL = "login"

DEFAULT_FROM_EMAIL = "КНТ МФТИ <info@knt-mipt.ru>"

# Шина событий чата (только prod, dev держит слой в памяти). socket_timeout ОБЯЗАТЕЛЕН:
# в redis-py 8 у него впервые появилось значение по умолчанию — 5с, ровно столько же
# channels_redis ждёт сообщение блокирующим чтением; таймеры гонятся и рвут сокет.
REDIS_HOSTS = [{"address": "redis://redis:6379/0", "socket_timeout": 30}]  # redis — имя сервиса из compose

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
PROXY = env("PROXY", default="")  # общий прокси для заблокированных ресурсов
