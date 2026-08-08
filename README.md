# Новый (опять) сайт факультета

Django 6 + PostgreSQL, серверный рендер + HTMX + Alpine, Tailwind v4 (standalone CLI, без Node).
Сайт закрытый: всё требует логина, кроме страниц входа и раздачи файлов.

Приложения: `core`, `users`, `chats`, `teachers`, `materials`, `library`, `attachments`,
`telegram`, `moderation`. Пакет настроек — `knt`.

---

# 1. Развёртывание на чистом сервере

Ubuntu 22.04/24.04, пользователь с `sudo`. Дальше он зовётся `$USER`, каталог проекта —
`/srv/test-knt`. Путь зашит в трёх местах: в `nginx-accel.conf` (alias на `media/`),
в `nginx.conf` (тот же alias для админки) и в `.github/workflows/deploy.yml`.
Меняешь путь — правь все три.

## 1.1. DNS

Две A-записи на IP сервера:

| Имя | Назначение | Cloudflare |
|---|---|---|
| `test.inbicst.ru` | сайт | **DNS only** (серое облако) |
| `files.inbicst.ru` | файлы и картинки | **DNS only** (серое облако) |

Оранжевое облако (proxied) ставить **нельзя ни на одну из них**. Российские провайдеры
душат Cloudflare: проходят первые ~16 КБ ответа, дальше поток обрывается. Именно из-за
этого мы и раздаём файлы сами, а не своим доменом на R2 (подробности — в разделе 4).

## 1.2. Пакеты

```bash
sudo apt update && sudo apt install -y nginx certbot git curl ca-certificates
```

Docker — официальным скриптом. Если `get.docker.com` не открывается, добавь `--proxy`:

```bash
curl -fsSL https://get.docker.com | sudo sh
```

Себя в группу `docker`, чтобы не писать `sudo` перед каждой командой (нужен релогин):

```bash
sudo usermod -aG docker "$USER"
```

Проверь, что есть модуль slice — без него кеш файлов не соберётся:

```bash
nginx -V 2>&1 | tr ' ' '\n' | grep slice
```

Должно вывести `--with-http_slice_module`. В сборке nginx из apt он есть.

## 1.3. Код и `.env`

```bash
sudo mkdir -p /srv/test-knt && sudo chown "$USER:$USER" /srv/test-knt
git clone https://github.com/Nepike/new-knt.git /srv/test-knt
cd /srv/test-knt && cp .env.example .env
```

`.env` в git не попадает и живёт **только на сервере**. Заполнить:

| Переменная | Чем |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `knt.settings.prod` |
| `SECRET_KEY` | `python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"` |
| `POSTGRES_PASSWORD` | любой длинный пароль |
| `DATABASE_URL` | `postgres://knt:ТОТ_ЖЕ_ПАРОЛЬ@db:5432/knt` |
| `EMAIL_HOST_PASSWORD` | пароль приложения ящика `info@knt-mipt.ru` |
| `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `FILES_BASE_URL` | `https://files.inbicst.ru` |
| `R2_BUCKET`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | из панели Cloudflare R2 |
| `R2_PREFIX` | пусто (прод пишет в корень бакета) |
| `PROXY` | прокси для заблокированного, нужен на сборке Tailwind |

> **Комментарий в конце строки уезжает в значение.** `django-environ` не режет `#`,
> и `TELEGRAM_BOT_TOKEN=123:abc # бот` даёт токен с пробелами — бот падает в цикле.
> Комментарий класть отдельной строкой над переменной.

## 1.4. nginx: что где лежит

Четыре файла, и каждый — в своём месте не случайно.

```
/etc/nginx/conf.d/websocket.conf        map $connection_upgrade   ─┐ уровень http,
/etc/nginx/conf.d/r2-cache.conf         proxy_cache_path + $r2_host ┘ в server нельзя
/etc/nginx/snippets/accel.conf          ← копия nginx-accel.conf из репозитория
/etc/nginx/sites-available/test.inbicst.ru  ← копия nginx.conf из репозитория
/etc/nginx/sites-enabled/test.inbicst.ru    → симлинк на строку выше
```

* **`conf.d/websocket.conf`** — `map` для WebSocket. Директива `map` живёт в контексте
  `http`, а не `server`, поэтому не может лежать в конфиге сайта. Она должна быть **одна
  на весь nginx**, иначе `duplicate map`. На нашем сервере такая уже есть в шапке
  `sites-available/default` — сперва проверь:

  ```bash
  grep -RIn connection_upgrade /etc/nginx/
  ```

  Если не нашлось ни одной:

  ```bash
  sudo tee /etc/nginx/conf.d/websocket.conf >/dev/null <<'EOF'
  map $http_upgrade $connection_upgrade { default upgrade; '' close; }
  EOF
  ```

* **`conf.d/r2-cache.conf`** — два куска, которым тоже нужен контекст `http`: зона кеша
  и адрес хранилища. Отдельным файлом ещё и потому, что тут id аккаунта Cloudflare —
  в репозитории ему не место. Подставь `R2_ACCOUNT_ID` из `.env` вместо `ACCOUNT`:

  ```bash
  sudo mkdir -p /var/cache/nginx/r2 && sudo chown www-data:www-data /var/cache/nginx/r2
  sudo tee /etc/nginx/conf.d/r2-cache.conf >/dev/null <<'EOF'
  proxy_cache_path /var/cache/nginx/r2 levels=1:2 keys_zone=r2:20m max_size=10g inactive=90d use_temp_path=off;
  map $host $r2_host { default "ACCOUNT.r2.cloudflarestorage.com"; }
  EOF
  ```

  `max_size=10g` — потолок кеша на диске; `keys_zone=r2:20m` — память под ключи, 20 МБ
  хватает примерно на 160 тысяч кусков.

* **`snippets/accel.conf`** — внутренние локации раздачи, `/__r2/` и `/__local/`. Отдельным
  файлом, потому что подключается **в оба** server-блока (сайт и домен файлов), а копипаста
  разъехалась бы. Это дословная копия `nginx-accel.conf` из репозитория:

  ```bash
  sudo mkdir -p /etc/nginx/snippets
  sudo cp /srv/test-knt/nginx-accel.conf /etc/nginx/snippets/accel.conf
  ```

* **`sites-available/test.inbicst.ru`** — оба server-блока, TLS и редиректы. Копия
  `nginx.conf` из репозитория. **Единственный источник правды — файл в репозитории**;
  на сервере руками не правим, а перезаливаем целиком.

Каталог для проверок certbot:

```bash
sudo mkdir -p /var/www/certbot
```

## 1.5. Сертификаты

Блоки `443` ссылаются на сертификаты, которых ещё нет, — с ними nginx не стартует.
Поэтому сперва кладём только часть файла до маркера `# ==== HTTPS`:

```bash
sed '/^# ==== HTTPS/,$d' /srv/test-knt/nginx.conf | sudo tee /etc/nginx/sites-available/test.inbicst.ru >/dev/null
sudo ln -s /etc/nginx/sites-available/test.inbicst.ru /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Выпускаем. `--webroot`, а не `--nginx`: плагин лезет переписывать конфиг, а нам этого
не надо — TLS уже описан в файле.

```bash
sudo certbot certonly --webroot -w /var/www/certbot -d test.inbicst.ru
sudo certbot certonly --webroot -w /var/www/certbot -d files.inbicst.ru
```

Теперь файл целиком:

```bash
sudo cp /srv/test-knt/nginx.conf /etc/nginx/sites-available/test.inbicst.ru
sudo nginx -t && sudo systemctl reload nginx
```

Продление автоматическое, таймером certbot. Способ проверки записан в
`/etc/letsencrypt/renewal/*.conf`, так что при продлении nginx не трогается.

## 1.6. Запуск

```bash
cd /srv/test-knt && docker compose up -d --build
```

Поднимутся пять сервисов: `db`, `redis`, `web`, `worker`, `bot`. Миграции и
`collectstatic` прогоняет сам `web` при старте (`entrypoint.sh`) — руками не надо.
У `worker` и `bot` entrypoint переопределён, поэтому за миграции они не дерутся.

Первый администратор:

```bash
docker compose exec web python manage.py createsuperuser
```

Спросит email, имя, фамилию, пароль.

## 1.7. Проверка

```bash
docker compose ps
docker compose exec web python manage.py storage_check
```

`storage_check` пишет в хранилище байты, читает обратно, показывает ссылку и удаляет —
если тут `S3Storage` и «хранилище работает», связка с R2 живая.

Дальше — раздача. Открой в браузере любой файл и посмотри заголовки: должен прийти
`X-Cache: MISS`, при повторном запросе `X-Cache: HIT`, а `Content-Type` — настоящий
(`application/pdf`, а не `text/html`).

```bash
curl -sI https://files.inbicst.ru/f/<токен>/<имя> | grep -i "x-cache\|content-type\|content-length"
```

## 1.8. Автодеплой

`.github/workflows/deploy.yml` на каждый push в `main` ходит по SSH и делает
`git pull --ff-only && docker compose up -d --build`, а в телеграм пишет старт/итог.
Секреты репозитория: `SSH_HOST`, `SSH_USER`, `SSH_KEY`, `TG_TOKEN`, `TG_CHAT_ID`,
`TG_TOPIC_ID`.

---

# 2. Разработка на Windows

## 2.1. Что поставить

* **Python 3.12** — с галкой «Add to PATH».
* **Git**.
* **PostgreSQL 17** — необязателен: без `DATABASE_URL` разработка живёт на SQLite.
  Ставится через `winget install PostgreSQL.PostgreSQL.17`, база и пользователь `knt`.
* **Docker Desktop** — нужен только ради Redis для очереди задач.

## 2.2. Окружение

```powershell
git clone https://github.com/Nepike/new-knt.git
cd new-knt
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Tailwind — отдельный бинарник, в git не хранится. Скачай
[tailwindcss-windows-x64.exe](https://github.com/tailwindlabs/tailwindcss/releases)
и положи в корень проекта как `tailwindcss.exe`.

## 2.3. `.env` для разработки

Скопируй `.env.example` в `.env` и оставь почти всё пустым:

```
DJANGO_SETTINGS_MODULE=knt.settings.dev
DATABASE_URL=postgres://knt:knt@localhost:5432/knt
```

Всё остальное можно не заполнять, и это осмысленно:

* `SECRET_KEY` в dev зашит в настройках;
* `R2_*` пусто — файлы ложатся в `media/` на диске, боевой бакет не трогается;
* `FILES_BASE_URL` пусто — файлы раздаются тем же адресом, что и сайт;
* `TELEGRAM_BOT_TOKEN` пусто — сообщения печатаются в окно воркера (`TELEGRAM_CONSOLE`);
* письма туда же, в окно воркера.

`DATABASE_URL` можно убрать вовсе — тогда возьмётся SQLite в `db.sqlite3`.

## 2.4. База и данные

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
```

Демо-контент (**только для разработки**): сперва заведи в админке хотя бы один предмет
и семестры, потом

```powershell
.\.venv\Scripts\python.exe manage.py seed_books
```

```powershell
.\.venv\Scripts\python.exe manage.py seed_materials
```

Убрать обратно — `--wipe` тем же командам.

## 2.5. Запуск: три окна

Tailwind следит за шаблонами и пересобирает css:

```powershell
.\tailwindcss.exe -i theme/input.css -o core/static/core/css/base.css --watch
```

Сам сайт:

```powershell
.\.venv\Scripts\python.exe manage.py runserver
```

Воркер очереди — в нём же печатаются письма и телеграм. На Windows обязателен
`--pool=solo`: обычный prefork форкает процесс, а Windows этого не умеет.

```powershell
.\.venv\Scripts\celery.exe -A knt worker -l info --pool=solo
```

Воркеру нужен Redis:

```powershell
docker run -d --name knt-redis -p 127.0.0.1:6379:6379 redis:8-alpine redis-server --save "" --appendonly no
```

Проверить всю связку «сайт → Redis → воркер»:

```powershell
.\.venv\Scripts\python.exe manage.py celery_check
```

Если воркер не запущен, письмо просто ждёт в очереди; если лежит сам Redis — уходит
напрямую, без очереди.

## 2.6. Тесты

```powershell
.\.venv\Scripts\python.exe manage.py test
```

Тесты пишут файлы во временный каталог и **никогда** в R2: хранилище подменяется у всех
файловых полей разом (`core/test_runner.py`). Фоновые задачи выполняются на месте.

## 2.7. Чем dev отличается от прода

| | dev | prod |
|---|---|---|
| База | SQLite или локальный Postgres | Postgres в контейнере |
| Файлы | `media/` на диске | R2 |
| Раздача файлов | редирект от Django | `X-Accel-Redirect`, байты отдаёт nginx |
| Домен файлов | тот же, что у сайта | `files.inbicst.ru` |
| Шина чата | слой в памяти | Redis |
| Письма и телеграм | печать в окно воркера | настоящая отправка |
| Статика | `runserver` | whitenoise внутри контейнера |
| Прямая загрузка в хранилище | недоступна (нужен R2) | работает |

---

# 3. Повседневные команды сервера

Из `/srv/test-knt`.

Выкатить новый код (обычно это делает CI сам):

```bash
docker compose up -d --build
```

Остановить, оставив контейнеры на месте:

```bash
docker compose stop
```

Поднять обратно:

```bash
docker compose start
```

Полная остановка — контейнеры удаляются, база и файлы остаются:

```bash
docker compose down
```

**Никогда не `docker compose down -v`** — флаг сносит тома вместе с базой.

После правки `.env` `restart` **не поможет**: окружение зашивается в контейнер при
создании. Нужно пересоздать:

```bash
docker compose up -d --force-recreate web worker bot
```

Логи и состояние:

```bash
docker compose logs -f --tail=100 web
```

```bash
docker compose ps
```

Разовая команда:

```bash
docker compose exec web python manage.py <команда>
```

Подобрать брошенные прямые загрузки (файл уехал в R2, а форму не сохранили):

```bash
docker compose exec web python manage.py clean_uploads --days 1 --apply
```

Обновить конфиг nginx после правки в репозитории:

```bash
sudo cp /srv/test-knt/nginx.conf /etc/nginx/sites-available/test.inbicst.ru && sudo nginx -t && sudo systemctl reload nginx
```

Почистить кеш файлов (например, если объект в бакете всё-таки подменили):

```bash
sudo rm -rf /var/cache/nginx/r2/* && sudo systemctl reload nginx
```

---

# 4. Файлы: как они раздаются

Всё, что загружают люди — файлы книг и материалов, картинки галереи и комментариев,
фото профилей и преподавателей, — лежит в одном бакете R2, а раздаётся **с нашего
сервера**, с домена `files.inbicst.ru`. Cloudflare в пути браузера нет.

```
браузер ──► files.inbicst.ru/img/<подпись>/
                │
                ├─► Django: проверил подпись, вернул ПУСТОЙ ответ
                │            с заголовком X-Accel-Redirect: /__r2/<бакет>/<ключ>?<подпись>
                │            (процесс свободен, байты через python не идут)
                │
                └─► nginx, локация /__r2/ (internal):
                      HIT  → отдал с диска, /var/cache/nginx/r2
                      MISS → сходил в R2 кусками по 1 МБ, положил в кеш, отдал
```

Байты через python не идут никогда — иначе воркер gunicorn держался бы всё скачивание.

**Кеш неограниченно долгий.** Объект по ключу неизменен: в ключе uuid, перезапись
запрещена (`file_overwrite=False`), — значит протухать нечему. Куски по 1 МБ нужны
из-за читалок pdf: они просят середину книги, а не файл целиком.

**В ключе кеша нет подписи** — она меняется на каждом рендере, и с ней попаданий не было
бы никогда. Ключ — это `$uri$slice_range`, то есть путь к объекту и номер куска.

**Отдельный хост, а не путь на сайте.** Это другой origin: куки сессии туда не уходят,
и файл, проскочивший мимо фильтра расширений, не дотянется до страниц сайта. Плата —
на `files.` нет сессии, поэтому разрешением служит подпись в самом адресе. Перебрать
библиотеку всё равно нельзя: токен подписан `SECRET_KEY`, в ключе uuid
(`attachments.storage.random_key`), листинг бакета закрыт.

## Почему не свой домен на самом R2

Пробовали, не работает. Домен R2 обязан быть проксирован через Cloudflare, а Cloudflare
с 9 июня 2025 душат российские провайдеры: проходят первые ~16 КБ, дальше поток
обрывается. Проверено подменой адресов — с того же IP посторонний хост качается за 0.8 с,
а `files.inbicst.ru` на любом IP Cloudflare умирает на 24 КБ; различается только имя
в SNI. Перевести запись R2 в **DNS only** нельзя: она управляемая, у бакета нет origin-IP.

Работающий сейчас `*.r2.cloudflarestorage.com` (туда ходит наш nginx) в список пока
не попал. Если попадёт — переезд на другое S3-совместимое хранилище стоит замены ключей
в `.env`: код ходит через `S3Storage`.

## Переключение хранилища

`R2_BUCKET` пуст — файлы ложатся в `media/` на диске; заполнен — в R2. Адреса снаружи
при этом одинаковые, меняется только внутренний переход: `/__local/` вместо `/__r2/`.
Переключение — рестарт: хранилище выбирается один раз при импорте моделей.

Прямая загрузка крупных файлов в обход приложения работает **только при включённом R2**
(`attachments.uploads.direct_upload`).

Полной копии бакета на диске мы не держим. Если файлы приехали не в то хранилище
(например, загружали, пока R2 лежал), их догоняют вручную:

```bash
python manage.py storage_sync --check
```

```bash
python manage.py storage_sync --push --apply
```

`--push` льёт с диска в R2, `--pull` — обратно. Без `--apply` только показывает.
Ключ у блоба в обоих хранилищах один и тот же, так что в базе менять нечего.

## Старые файлы

Загруженные до перехода на uuid лежат по предсказуемым путям вида
`books/12/files/Зорич.pdf` — такую библиотеку можно перебрать снаружи. Разово перевести:

```bash
python manage.py rekey_media --apply
```

Без `--apply` только показывает. Новые загрузки приходят с uuid сразу.

---

# 5. Что читать, чтобы понять раздачу файлов

По порядку. Пять файлов, примерно 300 строк — этого хватает целиком.

### 1. `attachments/storage.py` — где лежат байты

Начинать отсюда: тут решается, в R2 или на диск. `media_storage()` — **функция**, а не
готовый объект, и это важно: миграция запоминает ссылку на функцию, поэтому dev и prod
живут на одной миграции с разными хранилищами. `random_key()` объясняет, почему адреса
неугадываемы. `media_fields()` и `connect_blob_cleanup()` — обход всех файловых полей
проекта: по нему же снимаются блобы при удалении записи.

### 2. `attachments/media.py` — какой адрес попадает в HTML

Ключевая мысль в шапке файла: наружу и картинка, и файл идут **по нашему адресу
с подписью**, а откуда взять байты — решает уже вьюха. Поэтому переключение хранилища
не меняет ни одной ссылки в разметке.

Второй повод для своего звена — подписанная ссылка R2 меняется на КАЖДОМ рендере
(в подпись входит время). Вклеенная в HTML, она и протухает, и убивает кеш браузера.
Смотри, почему `Signer`, а не `TimestampSigner`.

### 3. `attachments/views.py`, функция `_deliver` — сердце всей схемы

Двадцать строк, в которых происходит вся магия. Django возвращает **пустой** ответ
с одним заголовком `X-Accel-Redirect`, а дальше файл забирает nginx. Обрати внимание
на две вещи:

* ветка `if not settings.MEDIA_ACCEL` — в разработке nginx нет, там остаётся обычный
  редирект, и это единственное отличие dev от прода в раздаче;
* `mimetypes.guess_type` — при `X-Accel-Redirect` заголовок приложения побеждает
  mime-тип nginx, и без этой строки браузер показывал бы pdf текстом, а картинку ничем.
  Это ловилось живым тестом, а не юнит-тестами.

### 4. `nginx-accel.conf` — то, что делает nginx

Локация `/__r2/`, снизу вверх по важности: `internal` (снаружи сюда не попасть),
`proxy_pass` через переменную и потому `resolver`, `Host` (входит в подпись SigV4 —
менять нельзя), `slice 1m`, и `proxy_cache_key` **без подписи**. Каждая строка
прокомментирована — читается за пять минут.

### 5. `nginx.conf` — куда это включено

Два server-блока: сайт и домен файлов. `include snippets/accel.conf;` стоит в обоих —
на случай, если `FILES_BASE_URL` пуст и адреса остались на домене сайта. Шапка файла —
инструкция по установке, она же раздел 1.4 выше.

### Дальше по желанию

* `attachments/uploads.py` — прямая загрузка в обход приложения: сервер подписывает
  ссылку и ключ, браузер льёт файл сам, потом форма присылает подписанный токен.
  Отвечает на вопрос «почему нельзя просто прислать ключ» (можно было бы прицепить
  к своей книге чужой объект из бакета).
* `attachments/tests.py`, класс `AccelTests` — те же утверждения, но исполняемые.
* `knt/settings/base.py`, блок `R2_OPTIONS` — почему `addressing_style: path`,
  `file_overwrite: False` и суточная подпись.

---

# 6. Фоновые задачи (Celery)

Всё, что ходит в чужую сеть и может подвиснуть, — почта, телеграм — уезжает в очередь,
чтобы запрос пользователя не ждал чужой сервер.

В проде воркер поднимается сервисом `worker` из docker-compose, Redis там уже есть.
Базы Redis разведены: **0** — шина чата, **1** — очередь задач, **2** — ответы задач.

Локальный запуск — см. раздел 2.5.
