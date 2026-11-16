from pathlib import Path

import environ
from celery.schedules import crontab

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
    "comments",
    "bookmarks",
    "library",
    "attachments",
    "intake",
    "lectorium",
    "telegram",
    "moderation",
    "economy",
    "cosmetics",
    "wall",
    "printing",
    "pulse",
    "notifications",
    "quest",
]
# daphne первым — иначе не перехватит runserver, и в dev не будет WebSocket.
INSTALLED_APPS = ["daphne"] + DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    # Первым: время и память меряются за весь путь запроса.
    "core.middleware.SlowRequestMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.HtmxMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.LockdownMiddleware",
    # Сайт закрыт: всё требует входа, кроме @login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "users.middleware.MustChangePasswordMiddleware",
    # После аутентификации: отметка активности ставится известному человеку.
    "users.sessions.ActivityMiddleware",
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
                "core.context_processors.section",
                "core.context_processors.site_theme",
                "chats.context_processors.unread_messages",
                "notifications.context_processors.unread_notifications",
                "cosmetics.context_processors.my_frame",
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

# Событийный скин: None — по календарю, имя — принудительно (dev.py; тесты — core/test_runner.py).
SITE_THEME = None

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
# Пустая строка — тоже «по умолчанию»: в .env переменная обычно без значения.
MEDIA_ROOT = Path(env("MEDIA_ROOT", default="") or BASE_DIR / "media")

# Прод переопределяет staticfiles на whitenoise (см. prod.py).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Выкаченный коммит: «3f2fc8f · 2026-10-26 · …». Ставит сборка образа (ARG KNT_VERSION), в dev пусто.
SITE_VERSION = env("KNT_VERSION", default="")

# Cloudflare R2. Пусто — локальный диск (dev).
R2_BUCKET = env("R2_BUCKET", default="")
# Домен раздачи файлов — другой origin: куки сайта туда не уходят. Пусто — домен сайта (dev).
FILES_BASE_URL = env("FILES_BASE_URL", default="")
# X-Accel-Redirect: файл отдаёт nginx, процесс свободен сразу. В dev nginx нет — обычный редирект.
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
    # Подпись читает наш nginx, но большой файл он тянет кусками — сутки с запасом.
    "querystring_expire": 24 * 3600,
    "file_overwrite": False,  # S3-хранилище иначе молча затирает одноимённый файл
    # Ключи уникальны и не переиспользуются, поэтому кешировать можно навсегда.
    "object_parameters": {"CacheControl": "public, max-age=31536000, immutable"},
}

TEST_RUNNER = "core.test_runner.MediaIsolatedRunner"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "users.User"
# Первым — шлагбаум ограничителя (users/backends.py). ModelBackend — прежним путём:
# на нём записаны живые сессии.
AUTHENTICATION_BACKENDS = ["users.backends.LoginGate", "django.contrib.auth.backends.ModelBackend"]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"  # корень ведёт в материалы; TODO: сменить на главную, когда появится
LOGOUT_REDIRECT_URL = "login"

DEFAULT_FROM_EMAIL = "КНТ МФТИ <info@knt-mipt.ru>"
# Письмо уходит в очередь; отправляет воркер бэкендом EMAIL_DELIVERY_BACKEND.
EMAIL_BACKEND = "core.mail.QueuedEmailBackend"
EMAIL_DELIVERY_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

# Кэш в dev — память процесса: форма входа не должна зависеть от Redis. Прод — Redis (prod.py).
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Шина каналов (только prod). socket_timeout больше блокирующего чтения channels_redis,
# иначе таймеры гонятся и рвут сокет.
REDIS_HOSTS = [{"address": "redis://redis:6379/0", "socket_timeout": 30}]  # redis — имя сервиса из compose

# Фоновые задачи: всё, что ходит в чужую сеть (почта, телеграм). Брокер — в dev.py и prod.py.
CELERY_TASK_IGNORE_RESULT = True  # почти всё «выстрелил и забыл»; кому нужен ответ — ignore_result=False
CELERY_RESULT_EXPIRES = 3600  # ответы редки и нужны недолго, копить их в Redis незачем
CELERY_TASK_SOFT_TIME_LIMIT = 60  # задача получает исключение и успевает прибраться
CELERY_TASK_TIME_LIMIT = 120  # не среагировала — воркер убивает процесс
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True  # Redis может подняться позже воркера
CELERY_WORKER_HIJACK_ROOT_LOGGER = False  # логирование настраивает Django (см. prod.LOGGING)
CELERY_TIMEZONE = TIME_ZONE  # расписания beat считаются по нему

# Расписание (beat). Ночью: воркер один, а уборка бакета занимает его надолго.
CELERY_BEAT_SCHEDULE = {
    # Короткие задачи — до долгой уборки бакета.
    "backup-database": {
        "task": "core.tasks.backup_database",
        "schedule": crontab(hour=3, minute=40),
    },
    "sweep-printing": {
        "task": "printing.tasks.sweep_printing",
        "schedule": crontab(hour=3, minute=50),
    },
    "sweep-journals": {
        "task": "core.tasks.sweep_journals",
        "schedule": crontab(hour=4, minute=0),
    },
    "sweep-storage": {
        "task": "attachments.tasks.sweep_storage",
        "schedule": crontab(hour=4, minute=20),
    },
    # Молчание агента печати видит только сайт. Протухшие проверки выбрасываем: нужна одна свежая.
    "watch-printers": {
        "task": "printing.tasks.watch_printers",
        "schedule": 60.0,
        "options": {"expires": 50},
    },
    # И молчание пекарни (intake/health.py): тревога — от получаса, пяти минут хватает.
    "watch-bakeries": {
        "task": "intake.tasks.watch_bakeries",
        "schedule": 300.0,
        "options": {"expires": 290},
    },
}

# Токен пекарни (docs/media-pipeline.md). Пусто — приёмка закрыта: её ручки открыты без входа.
INTAKE_TOKEN = env("INTAKE_TOKEN", default="")

# Токен агента печати (docs/printing.md), отзывается отдельно от пекарни. Пусто — закрыто.
PRINT_AGENT_TOKEN = env("PRINT_AGENT_TOKEN", default="")
PRINT_WEEKLY_PAGES = 100  # сторон A4 в неделю на человека; свой предел — в PrintAccount
# Внешний адрес сети М4: с него человек «в здании», иначе только предупреждение.
PRINT_M4_IPS = ("194.50.24.117",)
PRINT_MAX_FILE_MB = 50
# Больше недельного лимита: печатают часто одну главу из толстого учебника.
PRINT_MAX_DOC_PAGES = 300

# Ящик аналитики (telegram/views.py), свой токен. Пусто — закрыт.
ANALYTICS_TOKEN = env("ANALYTICS_TOKEN", default="")

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CONSOLE = False  # dev печатает сообщения вместо отправки, как и письма
PROXY = env("PROXY", default="")  # общий прокси для заблокированных ресурсов
