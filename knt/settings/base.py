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
    "telegram",
    "moderation",
    "economy",
    "wall",
]
# daphne первым во всём списке — иначе не перехватит runserver и в разработке не будет WebSocket
INSTALLED_APPS = ["daphne"] + DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# Бета: наружу открыта только часть разделов, список в core/beta.py. Снять — поставить False.
BETA = True

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
    # После логина: анонимного сначала отправляем входить, а не объясняем ему про бету.
    "core.middleware.BetaLockMiddleware",
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
                "core.context_processors.beta",
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
# Путь машинно-зависимый, как и адрес базы: на время переноса старого сайта медиа
# уезжает на другой диск, где хватает места под 50+ ГБ.
# Пустая строка тоже значит «по умолчанию»: в .env переменная обычно объявлена без значения.
MEDIA_ROOT = Path(env("MEDIA_ROOT", default="") or BASE_DIR / "media")

# Прод переопределяет staticfiles на whitenoise (см. prod.py).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Cloudflare R2 для файлов книг и материалов (attachments.storage.media_storage).
# Пусто — работаем с локальным диском, так живёт разработка.
R2_BUCKET = env("R2_BUCKET", default="")
# Домен, с которого раздаются пользовательские файлы. Это ДРУГОЙ origin: куки сайта
# туда не уходят, и проскочивший мимо фильтра html не дотянется до страниц сайта.
# Пусто — тем же доменом, что и сайт (так живёт разработка).
FILES_BASE_URL = env("FILES_BASE_URL", default="")
# nginx умеет X-Accel-Redirect: файл он забирает и кеширует сам, наш процесс
# освобождается сразу. В разработке nginx нет — там остаётся обычный редирект.
MEDIA_ACCEL = False
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
    # Подписью пользуется наш же nginx, и тут же, — но большой файл он тянет кусками,
    # и на медленном канале это растягивается. Сутки с запасом.
    "querystring_expire": 24 * 3600,
    "file_overwrite": False,  # S3-хранилище иначе молча затирает одноимённый файл
    # Ключи уникальны и не переиспользуются, поэтому кешировать можно навсегда.
    "object_parameters": {"CacheControl": "public, max-age=31536000, immutable"},
}

TEST_RUNNER = "core.test_runner.MediaIsolatedRunner"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "users.User"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"  # корень ведёт в материалы; TODO: сменить на главную, когда появится
LOGOUT_REDIRECT_URL = "login"

DEFAULT_FROM_EMAIL = "КНТ МФТИ <info@knt-mipt.ru>"
# Django отдаёт письмо в очередь, а настоящей отправкой занимается воркер —
# бекендом из EMAIL_DELIVERY_BACKEND (в dev он переопределён на консоль).
EMAIL_BACKEND = "core.mail.QueuedEmailBackend"
EMAIL_DELIVERY_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

# Шина событий чата (только prod, dev держит слой в памяти). socket_timeout задаём явно:
# он должен быть заметно больше, чем channels_redis ждёт сообщение блокирующим чтением,
# иначе таймеры гонятся и рвут сокет (ловили это на redis-py с дефолтом в 5с).
REDIS_HOSTS = [{"address": "redis://redis:6379/0", "socket_timeout": 30}]  # redis — имя сервиса из compose

# Фоновые задачи. Всё, что ходит в чужую сеть и может подвиснуть, — почта, телеграм —
# уезжает сюда, чтобы запрос пользователя не ждал чужой сервер. Адрес брокера в dev/prod.
CELERY_TASK_IGNORE_RESULT = True  # почти всё «выстрелил и забыл»; кому нужен ответ — ignore_result=False
CELERY_RESULT_EXPIRES = 3600  # ответы редки и нужны недолго, копить их в Redis незачем
CELERY_TASK_SOFT_TIME_LIMIT = 60  # задача получает исключение и успевает прибраться
CELERY_TASK_TIME_LIMIT = 120  # не среагировала — воркер убивает процесс
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True  # Redis может подняться позже воркера
CELERY_WORKER_HIJACK_ROOT_LOGGER = False  # логирование настраивает Django (см. prod.LOGGING)
CELERY_TIMEZONE = TIME_ZONE  # понадобится расписаниям beat: crontab считает по нему

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CONSOLE = False  # dev печатает сообщения вместо отправки, как и письма
PROXY = env("PROXY", default="")  # общий прокси для заблокированных ресурсов
