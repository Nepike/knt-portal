# КНТ MIPT — student portal

Student portal for КНТ at MIPT, live at [knt-mipt.ru](https://knt-mipt.ru). Django 6 on
PostgreSQL, WebSockets over Channels, background work in Celery, deployed as Docker Compose from a
push to `main`.

> **In active development.** First commit June 2026, still moving. The site is serving its users on
> the production domain, every section open. It replaces the first generation of the same site,
> [knt-portal-legacy](https://github.com/Nepike/knt-portal-legacy) — the `import_legacy*`
> management commands exist to carry data across.

The whole site is behind a login: `LoginRequiredMiddleware` is global and pages opt out explicitly,
so there is nothing to see without an account.

## Stack

Python 3.12 · Django 6.0 · PostgreSQL (psycopg 3) · Redis · Celery · Channels + Daphne ·
Cloudflare R2 through django-storages · HTMX + Tailwind 4 · Docker Compose · nginx ·
GitHub Actions

## Applications

| App | What it holds |
|---|---|
| `users` | Custom user model, sessions with a last-seen marker, forced password change on first login, the people directory |
| `core` | Shared base: `Moderated` abstract model, subjects, terms, teams, name search, list filtering, sidebar sections, mail, throttling, HTMX middleware |
| `wall` | A pixel canvas in the spirit of r/place — boards, pixels, placements, protected areas, live over WebSockets |
| `chats` | Chats with membership, messages and reactions, live over WebSockets |
| `materials` | Uploaded study materials, going through moderation |
| `comments` | Discussion under a material or a lecture: threads, likes, images, anonymity |
| `bookmarks` | "Come back to this": a mark on a material, book, lecture course or teacher, and the page listing them |
| `library` | Book catalogue, also moderated |
| `teachers` | Teacher cards and student reviews |
| `economy` | Internal currency: a wallet per user, every change written to a ledger |
| `cosmetics` | What the currency buys — avatar frames and profile headers, five rarity tiers |
| `attachments` | Files and images, local disk or R2, including direct browser-to-bucket upload |
| `intake` | What a finished media file must be, plus the job queue the off-site bakery works from |
| `lectorium` | Video lectures: playlists that go through review, an HLS player, no bytes of its own |
| `moderation` | The review queue over everything that inherits `Moderated` |
| `telegram` | Bot: notifications into configurable chats and topics |

## Notes on the parts that are not obvious

**Deployment is a push.** `git push` to `main` triggers a workflow that SSHes to the server, pulls,
and runs `docker compose up -d --build`. Telegram gets three messages — started (with the commit
list), finished with the elapsed time, or failed with a link to the run log.

**Redis does four jobs on four databases.** 0 is the Channels bus, 1 the Celery queue, 2 its result
backend, 3 the cache that rate limiters live on. Separate numbers so that flushing one does not
take out the others. In development none of it is required: the channel layer falls back to the
in-memory one and the cache to local memory, so a missing Redis container cannot stop the login
page from rendering.

**Mail and Telegram go through the queue.** `EMAIL_BACKEND` hands the message to Celery and the
worker does the actual SMTP, so a slow mail server never blocks a request. Development swaps the
delivery backend for one that prints to the console — the message still travels the full path
through the queue.

**A `beat` container is the alarm clock.** It runs no work of its own, it only drops scheduled
tasks into the same queue (`CELERY_BEAT_SCHEDULE`). Today that is one job: a nightly
`clean_uploads --apply` that sweeps abandoned uploads and lecture folders nothing points at.
Its state file lives in the container's `/tmp` on purpose — the only thing persisting it would
buy is a catch-up run after a restart, and a storage sweep has nothing to catch up on.

**Files can live in two places.** With R2 credentials set they go to the bucket, and large uploads
go straight from the browser to it; with the variables empty they land in `media/` on disk, which
is how development runs so that it never touches the production bucket. `storage_sync` moves what
is already uploaded between the two. In production nginx serves the bytes and Django only returns
the header (`MEDIA_ACCEL`).

**The wall keeps a timelapse.** Board dimensions are capped at 255 because the replay journal
writes three bytes per event — x, y, colour — and a wider board would not fit that format. A
partial unique constraint guarantees exactly one active board.

**The wallet is its own row, not a field on the user.** Several places in the code call a plain
`user.save()`, which writes every field at once; had the balance been a user field, such a save
would silently undo a debit that happened a moment earlier.

## Tests

851 tests. A custom runner (`core/test_runner.py`) points every `FileField` at a temporary
directory before anything runs, so a newly added file field can never write into the live bucket
by accident; it also makes Celery eager. One test builds the static files
the way the production container does, because a dangling reference inside a vendored `.js` fails
`collectstatic` and stops the container from starting at all.

```bash
python manage.py test
```

On the SQLite fallback about a dozen fail, all of them name search. The cause is SQLite, not the
code: its `lower()` is ASCII-only, so `lower('Максим')` comes back unchanged while Python has
already lowercased the query, and `core/search.py` compares the two. PostgreSQL, which the project
actually runs on, lowercases Cyrillic.

## Running locally

```bash
export DJANGO_SETTINGS_MODULE=knt.settings.dev
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

No `.env` is needed for that: every setting the development profile reads has a default, so it
comes up on SQLite with an in-memory channel layer and console mail, and nothing else has to be
running. Redis is still wanted for Celery — `docker compose up redis` is enough. Copy
`.env.example` when you want the real database, R2 or the Telegram bot.

Stylesheets are the one thing not in the repository: `core/static/core/css/base.css` is generated
by Tailwind (the Dockerfile builds it in a separate stage), so a local run without
`tailwindcss -i theme/input.css -o core/static/core/css/base.css` renders unstyled. Seed commands
(`seed_books`, `seed_materials`) fill the catalogues with something to look at.

The cosmetics catalogue travels as a fixture instead: `manage.py loaddata cosmetics` installs the
frames, headers and backgrounds. Only rows — the files themselves already sit in the shared R2
bucket the fixture points at, so it is the same catalogue everywhere and no re-upload is involved.
Primary keys are kept on purpose, which makes a second run an update rather than a duplicate.
