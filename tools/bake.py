#!/usr/bin/env python3
"""Пекарня: печёт медиафайл по требованиям сайта (`docs/media-pipeline.md`).

    python tools/bake.py background zakat.mov
    python tools/bake.py header polosa.mp4 -o готовое

Только стандартная библиотека — запускается на любой машине с python и ffmpeg. Из проекта
берёт лишь `intake.spec` и `intake.mp4`, оба без Django. Требования — с сайта
(`GET /intake/spec/`); не достучались — скажем вслух и испечём по локальной копии.

Настройки — аргументом, переменной окружения или строкой в `.env` репозитория, в этом
порядке: `INTAKE_URL` (адрес сайта) и `INTAKE_TOKEN`.
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # запускают файлом, а не модулем — корень сам не появится
    sys.path.insert(0, str(ROOT))

from intake import mp4, spec  # noqa: E402 — только после правки sys.path

NVENC = "h264_nvenc"
# Шкала качества у libx264 (-crf) и NVENC (-cq) общая: 0 — лучше некуда, 51 — мыло.
QUALITY = 23
# Не влезли в потолок веса — жмём сильнее и пробуем снова: на восьмисекундном ролике заход стоит
# секунду. TODO (лекторий): для лекции битрейт считать от длительности, а не подбирать.
STEP = 4
TRIES = 3

# У лекции потолка веса нет — число сразу разумное. Разное у кодировщиков: `-cq 23` у NVENC
# даёт вдвое тяжелее `-crf 23` у libx264.
LECTURE_QUALITY = {NVENC: 30, "libx264": 26}
# Обложка — с десятой части записи: в первую секунду обычно пустая доска.
POSTER_AT = 0.10
# Шумоподавление: luma_spatial:chroma_spatial:luma_tmp:chroma_tmp. Первое низкое: чистка
# яркости в пространстве размывает штрихи мела. Снять совсем — `--denoise none`.
DENOISE = "1:2:3:5"

# Ссылки на заливку — порциями, куски — по несколько разом: упираемся в рукопожатия.
SIGN_BATCH = 200
# Потоков заливки: кусок весит мегабайт-другой, и на каждый — своё соединение.
UPLOAD_THREADS = 12
# Попыток на кусок: из двух с половиной тысяч хоть один ответ теряется почти наверняка.
PUT_TRIES = 4

# Кроме Ctrl+C пекарню останавливают SIGTERM (systemd) и SIGHUP (tmux, закрытая консоль) —
# то же самое: иначе задание висит «обрабатывается» до `intake.models.CLAIM_TIMEOUT`.
STOP_SIGNALS = [name for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)]

# Как часто отчитываться о ходе: в консоли строка переписывает себя, в логе — реже, отдельными строками.
PULSE = 5
QUIET = 30
# Сырьё тянем потоком, кусками.
CHUNK = 4 * 1024 * 1024
# Как часто говорить сайту «жива»: срок задания идёт от последней вести.
BEAT = 300
# Меньше свободного места — работу не берём: сырьё бывает 16 ГБ, рядом ложится набор.
MIN_FREE = 40 * 1024 ** 3


def say(text=""):
    print(text, flush=True)


def human(size):
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} КБ"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} МБ"
    return f"{size / 1024 ** 3:.2f} ГБ"


def spell(seconds):
    """Промежуток словами — и для «прошло», и для «осталось»."""
    if seconds < 60:
        return f"{seconds:.1f} с"
    return f"{int(seconds) // 60} мин {int(seconds) % 60:02d} с"


def took(started):
    """Сколько прошло с отметки `time.monotonic()`."""
    return spell(time.monotonic() - started)


def rate(size, started):
    """Скорость сетевой фазы: чинить пекарню или звонить провайдеру."""
    return f"{size / max(time.monotonic() - started, 0.001) / 1024 ** 2:.1f} МБ/с"


def times(seconds, started):
    """«×5.9» — во сколько раз выпечка обгоняет реальное время (как `speed=` у ffmpeg)."""
    return f"×{seconds / max(time.monotonic() - started, 0.001):.1f}"


class Pulse:
    """Отчёт о ходе долгой фазы: сколько сделано и сколько осталось — иначе фаза неотличима
    от зависшей пекарни. В живой консоли строка переписывает себя, в логе — отдельными строками
    и реже (`isatty`). `unit` — как называть количество, `pace` — скорость.
    """

    def __init__(self, total, unit=human, pace=None):
        self.total, self.unit, self.pace = total, unit, pace
        self.started = self.last = time.monotonic()
        self.live = sys.stdout.isatty()
        self.every = PULSE if self.live else QUIET
        self.shown = False

    def show(self, done):
        now = time.monotonic()
        if done <= 0 or now - self.last < self.every:
            return
        self.last, passed = now, now - self.started
        parts = [f"{self.unit(done)} из {self.unit(self.total)}" if self.total else self.unit(done)]
        if self.total:
            parts.append(f"{done / self.total * 100:.0f}%")
        if self.pace:
            parts.append(self.pace(done, self.started))
        if self.total > done:
            parts.append(f"осталось ~{spell((self.total - done) * passed / done)}")
        self.shown = True
        line = "    " + " · ".join(parts)
        print(f"{line}\r" if self.live else line, end="" if self.live else "\n", flush=True)

    def clear(self):
        """Убрать переписываемую строку: следом печатается итог фазы."""
        if self.shown and self.live:
            print("\r" + " " * 79 + "\r", end="", flush=True)


def dotenv():
    """Строки из `.env` репозитория — как есть, с хвостом после решётки, как у django-environ."""
    path = ROOT / ".env"
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def setting(name, given=None):
    return given or os.environ.get(name) or dotenv().get(name, "")


def fetch(site, token):
    """Требования с сайта. Ошибку не глотаем — её печатает вызвавший."""
    address = site.rstrip("/") + "/intake/spec/"
    request = urllib.request.Request(address, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=10) as answer:
        return json.load(answer)


def token_problem(token):
    """Почему таким токеном не сходить, или None — мерка сайта (`core/agents.py:unusable`)."""
    if not token.isascii() or not token.isprintable() or " " in token:
        return "INTAKE_TOKEN не латиницей или с пробелом, такой заголовок не отправить"
    return None


def recipes(site, token, offline):
    """(рецепты, откуда взяты). Локальная копия — запасной путь, о нём говорим вслух."""
    if offline or not site or not token:
        why = "офлайн" if offline else "не задан INTAKE_URL или INTAKE_TOKEN"
        return spec.RECIPES, f"локальная копия репозитория ({why})"
    if problem := token_problem(token):
        say(f"  ! {problem} — беру локальную копию, она может быть старше")
        return spec.RECIPES, "локальная копия репозитория"
    try:
        raw = fetch(site, token)
    except (urllib.error.URLError, OSError, ValueError) as error:
        say(f"  ! сайт не ответил ({error}) — беру локальную копию, она может быть старше")
        return spec.RECIPES, "локальная копия репозитория"
    return {name: spec.recipe_from(data) for name, data in raw.items()}, site


def pick(known, asked):
    """Рецепт по имени: «background» вместо «cosmetic-background»."""
    for name in (asked, f"cosmetic-{asked}"):
        if name in known:
            return name, known[name]
    raise SystemExit(f"нет такого рецепта: {asked}. Есть: {', '.join(sorted(known))}")


def run(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def timecode(text):
    """«00:01:23.456789» → 83.5 — ход ffmpeg. В первые доли секунды там «N/A» — тогда None."""
    try:
        hours, minutes, seconds = text.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def follow(lines, pulse, beat=None):
    """Скормить пульсу ход выпечки (ffmpeg сыплет «ключ=значение»); отсюда же — весточка
    сайту. Отдельно от запуска — чтобы проверялось без ffmpeg."""
    for line in lines:
        key, _, value = line.strip().partition("=")
        if key == "out_time" and (at := timecode(value)) is not None:
            pulse.show(at)
            if beat:
                beat()


def watch(args, seconds, beat=None):
    """Запустить ffmpeg и показывать ход; ответ — как у `run`. stderr — в файл: читать два
    конвейера разом — способ встать насмерть. Убиваем сами: с `Popen` SIGTERM приходит только
    питону, и брошенный ffmpeg грел бы карту до утра."""
    pulse = Pulse(seconds, unit=spell, pace=times)
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as log:
        # -progress — ход в stdout, -nostats убирает его из stderr; общие настройки — первыми.
        process = subprocess.Popen([args[0], "-progress", "pipe:1", "-nostats", *args[1:]],
                                   stdout=subprocess.PIPE, stderr=log,
                                   text=True, encoding="utf-8", errors="replace")
        try:
            follow(process.stdout, pulse, beat)
            code = process.wait()
        except BaseException:  # noqa: BLE001 — прерывание в том числе, за ним и заведено
            process.kill()
            process.wait()
            raise
        finally:
            pulse.clear()
        log.seek(0)
        return subprocess.CompletedProcess(args, code, "", log.read())


def find_ffmpeg():
    if found := shutil.which("ffmpeg"):
        return found
    try:  # запасной путь: колесо с готовым бинарником, ставится обычным pip
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def nvenc_problem(ffmpeg):
    """Почему видеокарту взять нельзя, или None. Проверяем делом — кодируем чёрные кадры:
    `-encoders` перечисляет вкомпилированное, а не работающее. 256×256 — у NVENC нижняя
    граница кадра. Жалобу отдаём как есть: нет драйвера, ffmpeg без nvenc и нет карты —
    разные починки."""
    done = run([
        ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi",
        "-i", "color=black:s=256x256:d=0.2", "-c:v", NVENC, "-f", "null", "-",
    ])
    if done.returncode == 0:
        return None
    for line in done.stderr.splitlines():
        # «[h264_nvenc @ 0x55f…] Cannot load libcuda.so.1» — метку адреса отрезаем.
        if text := line.split("] ", 1)[-1].strip():
            return text
    return "ffmpeg не смог завести NVENC и не сказал почему"


def no_card(trouble):
    """Почему лекцию тут печь нельзя, или None. Без карты двухчасовая запись печётся часами,
    и задание уедет другой машине — лучше не начинать. Косметику это не касается. `--cpu` —
    решение человека: тогда беда не выясняется (`main`) и сюда приходит пустой."""
    if not trouble:
        return None
    return (f"видеокарта недоступна: {trouble}\n"
            "  Лекции без неё не печём. Настоять — ключ --cpu.")


def encoder_name(encoder):
    """Кодировщик для человека — первой строкой: молчаливый откат на процессор — часы вместо минут."""
    return f"{encoder}{' — на видеокарте' if encoder == NVENC else ''}"


def _fps(text):
    """«60/1» → 60.0: так ffprobe отдаёт частоту (у 29.97 знаменатель не единица)."""
    top, _, bottom = str(text or "0").partition("/")
    try:
        return float(top) / float(bottom or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def source_info(ffmpeg, source):
    """Что за исходник, или None, если спросить нечем. Лекции нужно: по высоте — дорожки,
    по частоте — резать ли её, по длительности — обложка и число сегментов. ffprobe есть
    не в каждой поставке (нет в `imageio-ffmpeg`)."""
    probe = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    if not probe.exists():
        return None
    done = run([
        str(probe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source),
    ])
    try:
        data = json.loads(done.stdout)
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        return {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "fps": _fps(video.get("r_frame_rate")),
            "duration": float(data["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in data["streams"]),
        }
    except (ValueError, KeyError, StopIteration):
        return None


def quality_args(encoder, quality):
    if encoder == NVENC:
        # У NVENC нет -crf: -cq работает только при -rc vbr и -b:v 0. -forced-idr — иначе
        # по -force_key_frames встают не IDR, и сегменты HLS выходят длиннее заказанных.
        return ["-c:v", NVENC, "-preset", "p5", "-rc", "vbr", "-cq", str(quality),
                "-b:v", "0", "-forced-idr", "1"]
    return ["-c:v", "libx264", "-crf", str(quality)]


def cut_at(recipe, fps):
    """До какой секунды резать, чтобы готовое не вышло ДЛИННЕЕ потолка: последний кадр
    ещё и длится (при 29.97 к/с `-t 8` даёт 8.008 с) — отступаем на кадр."""
    return recipe.seconds - 1 / fps if fps else recipe.seconds


def encode(ffmpeg, source, target, recipe, encoder, quality, cut):
    # Вписываем с обрезкой: исходник ровно 6:1 почти не бывает, scale расплющил бы шапку.
    fit = (f"scale={recipe.width}:{recipe.height}:force_original_aspect_ratio=increase,"
           f"crop={recipe.width}:{recipe.height}")
    args = [
        ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(source),
        "-t", f"{cut:.4f}", "-vf", fit,
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        # Оглавление вперёд — главное требование спеки.
        "-movflags", "+faststart",
    ]
    if not recipe.audio:
        args.append("-an")
    return run(args + quality_args(encoder, quality) + [str(target)])


def make_poster(ffmpeg, video, target):
    """Постер — ПЕРВЫЙ КАДР ГОТОВОГО файла: другая картинка дала бы скачок на загрузке.
    Машине это не проверить, правило держится здесь."""
    return run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(video),
                "-frames:v", "1", "-q:v", "3", str(target)])


def bake(ffmpeg, encoder, source, name, recipe, out):
    """Печёт и возвращает (видео, постер). Кидает SystemExit с текстом причины."""
    out.mkdir(parents=True, exist_ok=True)
    # Рецепт в имени: шапка и фон из одного исходника иначе затирали бы друг друга.
    video = out / f"{source.stem}-{name}.mp4"
    poster = out / f"{source.stem}-{name}.jpg"
    about = source_info(ffmpeg, source)
    cut = cut_at(recipe, about["fps"] if about else 0)

    for attempt in range(TRIES):
        quality = QUALITY + attempt * STEP
        done = encode(ffmpeg, source, video, recipe, encoder, quality, cut)
        if done.returncode:
            raise SystemExit(f"ffmpeg не справился:\n{done.stderr.strip()}")
        size = video.stat().st_size
        if size <= recipe.max_bytes or attempt == TRIES - 1:
            break
        say(f"  {human(size)} — тяжелее {human(recipe.max_bytes)}, жму сильнее (качество {quality + STEP})")

    done = make_poster(ffmpeg, video, poster)
    if done.returncode:
        raise SystemExit(f"постер не вынулся:\n{done.stderr.strip()}")
    return video, poster


def rungs(ladder, height):
    """Какие дорожки печь: вверх не растягиваем; ниже нижней ступени — одна дорожка как есть.
    Высота чётная — этого требует yuv420p."""
    return [step for step in ladder.heights if step <= height] or [height - height % 2]


def lecture_filter(heights, out_fps, source_fps, denoise):
    """Цепочка фильтров: почистить и разветвить НА ОДНОМ декодировании — чистка самая дорогая."""
    steps = []
    if source_fps > out_fps:  # ровно наоборот было бы дорисовыванием кадров из ничего
        steps.append(f"fps={out_fps}")
    if denoise:
        steps.append(f"hqdn3d={denoise}")
    head = "[0:v]" + "".join(f"{step}," for step in steps)

    if len(heights) == 1:
        return f"{head}scale=-2:{heights[0]}[v0]"
    taps = "".join(f"[s{i}]" for i in range(len(heights)))
    parts = [f"{head}split={len(heights)}{taps}"]
    parts += [f"[s{i}]scale=-2:{height}[v{i}]" for i, height in enumerate(heights)]
    return ";".join(parts)


def encode_ladder(ffmpeg, source, out, ladder, heights, about, encoder, quality, denoise, beat=None):
    out_fps = min(ladder.fps, about["fps"]) or ladder.fps
    audio = about["audio"]
    for index in range(len(heights)):
        (out / str(index)).mkdir(parents=True, exist_ok=True)

    args = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(source),
            "-filter_complex", lecture_filter(heights, out_fps, about["fps"], denoise)]
    variants = []
    for index in range(len(heights)):
        args += ["-map", f"[v{index}]"]
        if audio:
            args += ["-map", "0:a:0"]  # дорожек звука может быть несколько, берём первую
        variants.append(f"v:{index},a:{index}" if audio else f"v:{index}")

    args += ["-pix_fmt", "yuv420p", "-profile:v", "high"]
    # Опорный кадр — строго на границе сегмента: выражение от ВРЕМЕНИ, а не -g от частоты (29.97).
    args += ["-force_key_frames", f"expr:gte(t,n_forced*{ladder.segment})"]
    args += quality_args(encoder, quality)
    if audio:
        args += ["-c:a", "aac", "-b:a", f"{ladder.audio_kbps}k", "-ac", "2"]

    args += [
        "-f", "hls", "-hls_time", str(ladder.segment), "-hls_playlist_type", "vod",
        # fMP4, а не TS: легче (у TS заголовок в каждом 188-байтовом пакете) и годится для DASH.
        "-hls_segment_type", "fmp4",
        # Сегмент — с опорного кадра: иначе нет переключения качества и точной перемотки.
        "-hls_flags", "independent_segments",
        "-var_stream_map", " ".join(variants), "-master_pl_name", spec.MASTER,
        # as_posix: эти имена ffmpeg кладёт В МАНИФЕСТ как URL — windows-путь сломал бы набор.
        "-hls_segment_filename", (out / "%v" / "seg%05d.m4s").as_posix(),
        (out / "%v" / "index.m3u8").as_posix(),
    ]
    # Единственный долгий запуск ffmpeg — за ним и следим.
    return watch(args, about["duration"], beat)


# Сколько раз печь заново битый набор: сбой разовый.
REBAKES = 1


def damaged(ffmpeg, out, count, seconds, beat=None):
    """Первая жалоба декодера на испечённый набор, или пустая строка. Каждая дорожка перед
    заливкой проходит декодер целиком, с манифеста: ffmpeg может отработать без ошибки,
    а сбой памяти, диска или карты оставит битый сегмент, на котором встанет плеер.
    Мусор внутри кадра, который декодер молча рисует, отсюда не виден.
    """
    for index in range(count):
        playlist = out / str(index) / "index.m3u8"
        done = watch([ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
                      "-i", playlist.as_posix(), "-f", "null", "-"], seconds, beat)
        complaint = done.stderr.strip()
        if done.returncode or complaint:
            first = complaint.splitlines()[0] if complaint else f"ffmpeg вышел с кодом {done.returncode}"
            return f"дорожка {index}: {first}"[:300]
    return ""


def encode_checked(ffmpeg, source, out, ladder, heights, about, encoder, quality, denoise, beat=None):
    """Испечь дорожки и убедиться, что они читаются; битый набор — испечь заново."""
    for attempt in range(REBAKES + 1):
        if attempt:
            for index in range(len(heights)):
                shutil.rmtree(out / str(index), ignore_errors=True)
        done = encode_ladder(ffmpeg, source, out, ladder, heights, about, encoder, quality, denoise, beat)
        if done.returncode:
            raise SystemExit(f"ffmpeg не справился:\n{done.stderr.strip()}")
        say("  проверяю набор декодером…")
        problem = damaged(ffmpeg, out, len(heights), about["duration"], beat)
        if not problem:
            return
        say(f"  набор вышел битым ({problem}){' — пеку заново' if attempt < REBAKES else ''}")
    raise SystemExit(f"набор выходит битым и после повтора: {problem}")


def _attributes(line):
    """Разбор `#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="a,b"`: запятые в кавычках не делим."""
    values, key, buffer, quoted = {}, "", "", False
    for char in line.partition(":")[2] + ",":
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            if key:
                values[key] = buffer
            key, buffer = "", ""
        elif char == "=" and not key and not quoted:
            key, buffer = buffer, ""
        else:
            buffer += char
    return values


def read_output(out):
    """Что вышло — по манифестам, как прочтёт сайт. Так ловится и оборванная выпечка."""
    lines = (out / spec.MASTER).read_text(encoding="utf-8").splitlines()
    renditions, waiting = [], None
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF"):
            waiting = _attributes(line)
        elif waiting is not None and line.strip() and not line.startswith("#"):
            uri = line.strip()
            # Пекарня живёт на Windows — обратная коса в манифесте сломала бы набор.
            if "\\" in uri:
                raise SystemExit(f"в манифесте windows-путь вместо адреса: {uri}")
            renditions.append(_read_rendition(out, uri, waiting))
            waiting = None
    return renditions


def _read_rendition(out, relative, attributes):
    playlist = out / relative
    lines = playlist.read_text(encoding="utf-8").splitlines()
    if "#EXT-X-ENDLIST" not in lines:
        raise SystemExit(f"манифест {relative} не закрыт — выпечка оборвалась")

    seconds, names, init = 0.0, [], None
    for line in lines:
        if line.startswith("#EXTINF:"):
            seconds += float(line[len("#EXTINF:"):].rstrip(","))
        elif line.startswith("#EXT-X-MAP:"):
            init = _attributes(line).get("URI", "")
        elif line.strip() and not line.startswith("#"):
            names.append(line.strip())

    pieces = [playlist.parent / name for name in names] + ([playlist.parent / init] if init else [])
    missing = [piece.name for piece in pieces if not piece.exists()]
    if missing:
        raise SystemExit(f"в манифесте {relative} есть куски, которых нет на диске: {missing[:3]}")

    width, _, height = attributes.get("RESOLUTION", "0x0").partition("x")
    return {
        "height": int(height), "width": int(width), "segments": len(names),
        "seconds": seconds, "bytes": sum(piece.stat().st_size for piece in pieces),
        "playlist": relative, "init": init,
    }


def bake_lecture(ffmpeg, encoder, source, name, ladder, out, denoise, beat=None):
    """Печёт набор HLS и возвращает описание готового."""
    about = source_info(ffmpeg, source)
    if about is None:
        raise SystemExit("для лекции нужен ffprobe — по нему выбираются дорожки и берётся обложка")

    out = out / f"{source.stem}-{name}"
    out.mkdir(parents=True, exist_ok=True)
    heights = rungs(ladder, about["height"])
    quality = LECTURE_QUALITY.get(encoder, LECTURE_QUALITY[NVENC])
    say(f"  дорожки:    {', '.join(f'{h}p' for h in heights)}"
        f"{'' if about['audio'] else ' (звука в исходнике нет)'}")

    encode_checked(ffmpeg, source, out, ladder, heights, about, encoder, quality, denoise, beat)

    poster = out / spec.POSTER
    # Не первым кадром, в отличие от косметики: тут это картинка в списке, а не подложка видео.
    at = max(1.0, about["duration"] * POSTER_AT)
    done = run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(source),
                "-frames:v", "1", "-vf", f"scale=-2:{max(heights)}", "-q:v", "3", str(poster)])
    if done.returncode:
        raise SystemExit(f"обложка не вынулась:\n{done.stderr.strip()}")

    renditions = read_output(out)
    made = {
        "recipe": name,
        "duration": max((r["seconds"] for r in renditions), default=0.0),
        "poster": poster.name, "poster_bytes": poster.stat().st_size,
        "master": spec.MASTER, "renditions": renditions,
    }
    (out / "manifest.json").write_text(json.dumps(made, ensure_ascii=False, indent=2), encoding="utf-8")
    return out, made


def verify(video, poster, recipe):
    """Свой результат — ТОЙ ЖЕ проверкой, что на сайте."""
    with video.open("rb") as handle:
        info = mp4.probe(handle)
    problems = [spec.check(recipe, info, video.stat().st_size)]
    if (weight := poster.stat().st_size) > recipe.poster_bytes:
        problems.append(f"постер тяжелее {human(recipe.poster_bytes)} ({human(weight)})")
    return info, [problem for problem in problems if problem]


def report_clip(video, poster, recipe):
    info, problems = verify(video, poster, recipe)
    say(f"  → {video}  {info['width']}×{info['height']}, {info['seconds']:.1f} с, "
        f"{human(video.stat().st_size)}, faststart {'есть' if info['faststart'] else 'НЕТ'}")
    say(f"  → {poster}  постер первым кадром, {human(poster.stat().st_size)}")
    return problems


def report_lecture(out, made, ladder):
    hours, rest = divmod(int(made["duration"]), 3600)
    say(f"  → {out}")
    for rendition in made["renditions"]:
        weight = rendition["bytes"]
        say(f"     {rendition['width']}×{rendition['height']}  "
            f"{rendition['segments']} сегментов, {human(weight)}, "
            f"{weight * 8 / made['duration'] / 1_000_000:.2f} Мбит/с")
    say(f"     обложка {human(made['poster_bytes'])}, "
        f"длительность {hours}:{rest // 60:02d}:{rest % 60:02d}, манифест manifest.json")
    return [problem for problem in [spec.check_ladder(ladder, made)] if problem]


# ── очередь: пекарня приходит за работой сама ─────────────────────────────────


def talk(site, token, where, body):
    """Запрос к приёмке. Ответ — разобранный JSON; отказ — исключение с текстом причины."""
    request = urllib.request.Request(
        site.rstrip("/") + where,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return json.load(answer)
    except urllib.error.HTTPError as error:
        # 404 на неизвестную ручку и 502 от nginx — HTML: без этой ветки упали бы на
        # JSONDecodeError вместо «сайт ответил 404».
        try:
            answer = json.loads(error.read() or b"{}")
        except ValueError:
            answer = {}
        detail = answer.get("error", "") if isinstance(answer, dict) else ""
        raise SystemExit(f"приёмка ответила {error.code}: {detail or error.reason}")


def tell(site, token, where, body):
    """Отчитаться о задании, не падая: отчёт идёт ПОСЛЕ поломки, часто без связи. Не дошло —
    задание вернётся в очередь само."""
    try:
        talk(site, token, where, body)
    except (SystemExit, OSError, ValueError) as error:
        say(f"  ! сайту не сказалось ({error}) — задание вернётся в очередь само, через час")


class Beat:
    """Весточка сайту: работа идёт. Срок задания — от последней вести; без весточек сайт
    отобрал бы задание у работающей машины. Стучит сама работа (скачивание, ход ffmpeg),
    а не таймер: таймер держал бы задание и за повисшей машиной. Не дошло — `tell` не падает.
    """

    def __init__(self, site, token, job_token):
        self.site, self.token, self.job = site, token, job_token
        self.last = time.monotonic()

    def __call__(self):
        if time.monotonic() - self.last < BEAT:
            return
        self.last = time.monotonic()
        tell(self.site, self.token, "/intake/alive/", {"token": self.job})


def fetch_source(url, target, beat=None):
    """Скачать сырьё из хранилища потоком, с отчётом по дороге. Сырья нет (HTTPError) — отказ,
    а не обрыв: иначе `release` крутил бы задание по кругу."""
    try:
        answer = urllib.request.urlopen(url, timeout=120)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise SystemExit("сырья нет в хранилище (404) — запись придётся загрузить заново") from error
        raise
    with answer, target.open("wb") as file:
        pulse = Pulse(int(answer.headers.get("Content-Length") or 0), pace=rate)
        while chunk := answer.read(CHUNK):
            file.write(chunk)
            pulse.show(file.tell())
            if beat:
                beat()
        pulse.clear()
    return target.stat().st_size


def put_file(url, path, content_type=None):
    """Положить один кусок, с повторами и растущей паузой. Тип — от сайта, как есть: он
    ложится в объект, а urllib без него подставил бы `x-www-form-urlencoded`."""
    body = path.read_bytes()
    headers = {"Content-Type": content_type} if content_type else {}
    for attempt in range(1, PUT_TRIES + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="PUT")
            with urllib.request.urlopen(request, timeout=120):
                return
        except Exception as error:  # noqa: BLE001 — сеть отказывает десятком разных способов
            if attempt == PUT_TRIES:
                raise OSError(f"{path.name} не залился за {PUT_TRIES} попытки: {error}") from error
            time.sleep(2 ** attempt)


def upload(site, token, job_token, folder):
    """Залить готовый набор прямо в хранилище → (кусков, байт). Ссылки порциями, куски — потоками."""
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    names = [path.relative_to(folder).as_posix() for path in files]
    beside = dict(zip(names, files))
    sizes = {name: path.stat().st_size for name, path in beside.items()}
    started, done, weight = time.monotonic(), 0, 0
    for start in range(0, len(names), SIGN_BATCH):
        batch = names[start:start + SIGN_BATCH]
        answer = talk(site, token, "/intake/sign/", {"token": job_token, "names": batch})
        urls, types = answer["urls"], answer.get("types") or {}
        with ThreadPoolExecutor(max_workers=UPLOAD_THREADS) as pool:
            for _ in pool.map(lambda n: put_file(urls[n], beside[n], types.get(n)), batch):
                done += 1
        weight += sum(sizes[name] for name in batch)
        say(f"    залито {done} из {len(names)} · {rate(weight, started)}")
    return done, weight


def checkup(ffmpeg, encoder, place=None):
    """Что мешает печь прямо сейчас, или пустая строка — перед каждым походом за работой:
    драйвер могут обновить без перезагрузки, и карта отвалится. Беда уходит сайту вместе
    с запросом за работой (intake.health)."""
    try:
        if encoder == NVENC and (trouble := nvenc_problem(ffmpeg)):
            return f"видеокарта недоступна: {trouble}"
        free = shutil.disk_usage(place or tempfile.gettempdir()).free
    except OSError as error:
        return f"самопроверка не удалась: {error}"
    if free < MIN_FREE:
        return f"на рабочем диске свободно {human(free)}, нужно хотя бы {human(MIN_FREE)}"
    return ""


def serve_once(site, token, ffmpeg, encoder, denoise, known, problem=""):
    """Взять одно задание и довести до конца. False — пора передохнуть: работы не было,
    оборвалась связь или печь сейчас нельзя (`problem`, его же узнаёт сайт)."""
    answer = talk(site, token, "/intake/claim/", {"worker": socket.gethostname(), "problem": problem})
    job = answer.get("job")
    if job and problem:
        # Сайт старше пекарни и о неполадке не знает — взятое возвращаем сразу.
        tell(site, token, "/intake/release/", {"token": job["token"]})
        return False
    if not job:
        return False

    say(f"задание #{job['id']} · {job['recipe']}")
    recipe = known.get(job["recipe"])
    if recipe is None:
        tell(site, token, "/intake/fail/", {"token": job["token"], "error": f"нет рецепта {job['recipe']}"})
        return True

    workshop = Path(tempfile.mkdtemp(prefix="bake-"))
    beat = Beat(site, token, job["token"])
    whole = time.monotonic()
    try:
        source = workshop / (job["name"] or "source")
        say("  качаю сырьё…")
        clock = time.monotonic()
        weight = fetch_source(job["source"], source, beat)
        say(f"  сырьё: {human(weight)} за {took(clock)} · {rate(weight, clock)}")

        clock = time.monotonic()
        out, made = bake_lecture(ffmpeg, encoder, source, job["recipe"], recipe,
                                 workshop / "out", denoise, beat)
        if problem := spec.check_ladder(recipe, made):
            raise SystemExit(problem)
        baking = time.monotonic() - clock
        say(f"  испеклось за {took(clock)}")

        plan = talk(site, token, "/intake/plan/", {"token": job["token"], "manifest": made})
        say(f"  папка: {plan['prefix']}")
        clock = time.monotonic()
        pieces, sent = upload(site, token, job["token"], out)
        say(f"  залито: {pieces} кусков, {human(sent)} за {took(clock)}")
        talk(site, token, "/intake/commit/", {"token": job["token"]})
        # Доля видеокарты: меньше половины — машина больше ждёт сеть, чем считает.
        say(f"  готово за {took(whole)}, из них на видеокарте "
            f"{baking / max(time.monotonic() - whole, 0.001) * 100:.0f}%")
    except SystemExit as error:
        say(f"  НЕ ВЫШЛО: {error}")
        tell(site, token, "/intake/fail/", {"token": job["token"], "error": str(error)[:300]})
    except OSError as error:
        # Оборвалась связь — лекция ни при чём: `release`, а не `fail`.
        say(f"  СВЯЗЬ ОБОРВАЛАСЬ: {error}")
        tell(site, token, "/intake/release/", {"token": job["token"]})
        # И сразу за работой не идём: вернули задание в голову очереди — взяли бы его снова.
        return False
    except KeyboardInterrupt:
        # Ctrl+C — не отказ: `release` (из `fail` задание вынимают руками).
        say(f"\n  остановлено на {took(whole)} — возвращаю задание в очередь")
        tell(site, token, "/intake/release/", {"token": job["token"]})
        raise
    except Exception as error:  # noqa: BLE001 — что угодно, лишь бы сказать это сайту
        say(f"  СЛОМАЛОСЬ: {error}")
        tell(site, token, "/intake/fail/", {"token": job["token"], "error": f"{type(error).__name__}: {error}"[:300]})
    finally:
        # Сырьё и готовое — десятки гигабайт: убираем.
        shutil.rmtree(workshop, ignore_errors=True)
    return True


def catch_stop():
    """Остановка сигналом — то же, что Ctrl+C: `serve_once` вернёт задание и уберёт диск.
    Заодно юниту не нужен `KillSignal=SIGINT`."""
    def stop(number, _frame):
        raise KeyboardInterrupt(signal.Signals(number).name)

    for name in STOP_SIGNALS:
        signal.signal(getattr(signal, name), stop)


def serve(args, ffmpeg, encoder, denoise, known, site, token):
    """Ждать заданий и печь их по одному."""
    if not site or not token:
        raise SystemExit("для очереди нужны INTAKE_URL и INTAKE_TOKEN")
    say(f"пекарня ждёт работы: {site}, опрос раз в {args.every} с")
    say(f"кодировщик: {encoder_name(encoder)}")
    complained = ""
    try:
        while True:
            problem = checkup(ffmpeg, encoder)
            # В журнал — только смену состояния.
            if problem != complained:
                say(f"  не беру работу: {problem}" if problem else "  снова в порядке, беру работу")
                complained = problem
            try:
                worked = serve_once(site, token, ffmpeg, encoder, denoise, known, problem)
            # Приёмка недоступна — не повод умирать. «Сайт не отвечает вовсе» — сырой URLError
            # (OSError), не SystemExit.
            except (SystemExit, OSError, ValueError) as error:
                say(f"  приёмка молчит: {error}")
                worked = False
            if args.once:
                return 0
            time.sleep(1 if worked else args.every)
    except KeyboardInterrupt:
        # Ctrl+C ловится и в ожидании, и внутри задания (тогда оно уже возвращено). Без трейсбека.
        say("пекарня остановлена")
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="bake", description="Печёт медиа по требованиям сайта. Нужен ffmpeg.",
        epilog="Примеры: python tools/bake.py lecture zapis.mkv │ python tools/bake.py --serve",
    )
    parser.add_argument("kind", metavar="вид", nargs="?",
                        help="рецепт: background, header, lecture (или полным именем)")
    parser.add_argument("source", metavar="исходник", nargs="?", type=Path)
    parser.add_argument("--serve", action="store_true", help="ждать заданий из очереди сайта")
    parser.add_argument("--once", action="store_true", help="с --serve: взять одно задание и выйти")
    parser.add_argument("--every", metavar="СЕК", type=int, default=60, help="как часто спрашивать работу")
    parser.add_argument("-o", "--out", metavar="КАТАЛОГ", type=Path, default=Path("baked"))
    parser.add_argument("--site", metavar="URL", help="откуда брать требования (иначе INTAKE_URL)")
    parser.add_argument("--token", metavar="ТОКЕН", help="иначе INTAKE_TOKEN")
    parser.add_argument("--offline", action="store_true", help="не спрашивать сайт, взять требования из репозитория")
    parser.add_argument("--cpu", action="store_true", help="печь на процессоре, даже если есть NVENC")
    parser.add_argument("--denoise", metavar="НАСТРОЙКА", default=DENOISE,
                        help=f"шумоподавление лекции, hqdn3d (по умолчанию {DENOISE}); none — не чистить")
    args = parser.parse_args(argv)

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("не нашёл ffmpeg — поставь его (winget install Gyan.FFmpeg) или pip install imageio-ffmpeg")

    site, token = setting("INTAKE_URL", args.site), setting("INTAKE_TOKEN", args.token)
    known, whence = recipes(site, token, args.offline)
    trouble = "" if args.cpu else nvenc_problem(ffmpeg)
    encoder = "libx264" if args.cpu or trouble else NVENC
    denoise = "" if args.denoise.lower() in ("none", "нет", "") else args.denoise

    if args.serve or args.once:
        # Очередь возит только лекции — отказ без карты на входе, а не у задания.
        if refusal := no_card(trouble):
            raise SystemExit(refusal)
        catch_stop()  # ставим здесь, а не в `serve`: подмена обработчиков — дело запуска
        return serve(args, ffmpeg, encoder, denoise, known, site, token)
    if not args.kind or not args.source:
        parser.error("нужны вид и исходник — или --serve, чтобы брать задания из очереди")
    if not args.source.exists():
        raise SystemExit(f"нет такого файла: {args.source}")
    name, recipe = pick(known, args.kind)
    if isinstance(recipe, spec.Ladder) and (refusal := no_card(trouble)):
        raise SystemExit(refusal)

    say(f"пекарня · {recipe.title} ({name})")
    say(f"  требования: {whence}")
    say(f"  кодировщик: {encoder_name(encoder)}")
    if about := source_info(ffmpeg, args.source):
        say(f"  исходник:   {about['width']}×{about['height']}, {about['fps']:.0f} к/с, {about['duration']:.1f} с")

    if isinstance(recipe, spec.Ladder):
        out, made = bake_lecture(ffmpeg, encoder, args.source, name, recipe, args.out, denoise)
        problems = report_lecture(out, made, recipe)
    else:
        video, poster = bake(ffmpeg, encoder, args.source, name, recipe, args.out)
        problems = report_clip(video, poster, recipe)

    if problems:
        say("  НЕ ПРОШЛО приёмку:")
        for problem in problems:
            say(f"    · {problem}")
        return 1
    say("  проверено той же меркой, что на сайте — можно сдавать")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)  # разовая выпечка, прерванная руками: код как у всей консоли
