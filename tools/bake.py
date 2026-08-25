#!/usr/bin/env python3
"""Пекарня: печёт медиафайл по требованиям сайта.

    python tools/bake.py background zakat.mov
    python tools/bake.py header polosa.mp4 -o готовое

Договор целиком — в `docs/media-pipeline.md`. Коротко: сайт не конвертирует, а
принимает по спеке; печёт эта штука на машине с видеокартой и приносит готовое.

Живёт в репозитории сайта, но **только на стандартной библиотеке** — ни venv, ни pip.
Тогда её запускает любая машина, где просто есть python, и единственная внешняя вещь —
сам ffmpeg. По той же причине из проекта берутся ровно два модуля, `intake.spec` и
`intake.mp4`, и оба намеренно без Django.

Требования спрашиваются у сайта (`GET /intake/spec/`), а не берутся из своей копии:
репозиторий на домашнем компьютере может быть недельной давности. Не достучались —
скажем об этом вслух и испечём по локальной копии.

Настройки — аргументом, переменной окружения или строкой в `.env` репозитория,
в этом порядке: `INTAKE_URL` (адрес сайта) и `INTAKE_TOKEN`.
"""

import argparse
import json
import os
import shutil
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
# Не влезли в потолок веса — жмём сильнее и пробуем снова. Иначе человек получал бы
# «тяжелее 8 МБ» и крутил бы ручки сам, а пекарня ровно за этим и заведена.
# На восьмисекундном ролике заход стоит секунду, так что лесенка дешевле расчётов.
# TODO (лекторий): часовой лекции три полных прохода не годятся — там битрейт надо
# считать от длительности и потолка, а не подбирать.
STEP = 4
TRIES = 3

# У лекции потолка веса нет, подбирать нечего — берём число сразу разумное. Разное
# у кодировщиков не от прихоти: на замере `-cq 23` у NVENC дал вдвое более тяжёлый
# файл, чем `-crf 23` у libx264, и общее число означало бы разное качество.
LECTURE_QUALITY = {NVENC: 30, "libx264": 26}
# Обложку берём на десятой части записи: в первую секунду обычно пустая доска.
POSTER_AT = 0.10
# Шумоподавление: luma_spatial:chroma_spatial:luma_tmp:chroma_tmp. Первое число низкое
# намеренно — именно пространственная чистка яркости размывает тонкие штрихи мела,
# а основную экономию дают временнáя и цветовая, которым мел безразличен. Проверить
# на исписанной доске пока не на чем, поэтому настройка мягкая; снять совсем —
# `--denoise none`.
DENOISE = "1:2:3:5"

# Заливка готового: ссылки берём порциями, куски льём по несколько разом. Каждый кусок
# маленький, и последовательно две тысячи ушли бы часами на одних рукопожатиях.
SIGN_BATCH = 200
UPLOAD_THREADS = 4


def say(text=""):
    print(text, flush=True)


def human(size):
    return f"{size / 1024:.0f} КБ" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} МБ"


def dotenv():
    """Строки из `.env` репозитория. Значение берём как есть, вместе с хвостом после
    решётки: django-environ его тоже не срезает, и расходиться с сайтом нельзя."""
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
    """Почему таким токеном не сходить, или None. Та же мерка, что у сайта
    (`intake/views.py:unusable`): заголовки HTTP переносят только латиницу без пробелов,
    и без этой проверки пекарня падала бы на 'latin-1 codec can't encode'."""
    if not token.isascii() or not token.isprintable() or " " in token:
        return "INTAKE_TOKEN не латиницей или с пробелом, такой заголовок не отправить"
    return None


def recipes(site, token, offline):
    """(рецепты, откуда взяты). Локальная копия — запасной путь, и о нём говорим вслух:
    молча испечь по устаревшим требованиям хуже, чем не испечь."""
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
    """Рецепт по имени. «background» короче, чем «cosmetic-background», и печатать
    полное имя каждый раз незачем."""
    for name in (asked, f"cosmetic-{asked}"):
        if name in known:
            return name, known[name]
    raise SystemExit(f"нет такого рецепта: {asked}. Есть: {', '.join(sorted(known))}")


def run(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def find_ffmpeg():
    if found := shutil.which("ffmpeg"):
        return found
    try:  # запасной путь: колесо с готовым бинарником, ставится обычным pip
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def has_nvenc(ffmpeg):
    """Проверяем делом, а не списком `-encoders`: там перечислено вкомпилированное,
    а работает оно только при живой карте и драйвере. Кодируем несколько чёрных кадров.

    256×256 не «на всякий случай»: у NVENC есть нижняя граница кадра, и на 64×64 он
    отвечает «Frame Dimension less than the minimum supported value» — проба на видимо
    живой карте выдавала бы, что видеокарты нет.
    """
    return run([
        ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi",
        "-i", "color=black:s=256x256:d=0.2", "-c:v", NVENC, "-f", "null", "-",
    ]).returncode == 0


def _fps(text):
    """«60/1» → 60.0. Так ffprobe отдаёт частоту, и у 29.97 знаменатель не единица."""
    top, _, bottom = str(text or "0").partition("/")
    try:
        return float(top) / float(bottom or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def source_info(ffmpeg, source):
    """Что за исходник приехал, или None, если спросить нечем.

    Ролику косметики это украшение (было бы что сказать человеку), а лекции —
    необходимость: по высоте выбираются дорожки, по частоте — надо ли её резать,
    по длительности — где брать обложку и сходится ли число сегментов.

    `ffprobe` лежит рядом с ffmpeg, но не в каждой поставке — например, в колесе
    `imageio-ffmpeg` его нет.
    """
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
        # У NVENC нет -crf. Качество задаёт -cq, но только при -rc vbr и -b:v 0:
        # с ненулевым битрейтом он держит битрейт, а -cq молча ни на что не влияет.
        #
        # -forced-idr обязателен там, где кадры расставляются по -force_key_frames:
        # без него NVENC ставит на этих местах обычные опорные кадры вместо IDR,
        # резать по ним HLS нельзя, и сегменты выходят длиннее заказанных.
        return ["-c:v", NVENC, "-preset", "p5", "-rc", "vbr", "-cq", str(quality),
                "-b:v", "0", "-forced-idr", "1"]
    return ["-c:v", "libx264", "-crf", str(quality)]


def encode(ffmpeg, source, target, recipe, encoder, quality):
    # Вписываем с обрезкой, а не растягиванием: исходник ровно 6:1 не бывает почти
    # никогда, и простой scale расплющил бы шапку. Обрезается лишнее по краям.
    fit = (f"scale={recipe.width}:{recipe.height}:force_original_aspect_ratio=increase,"
           f"crop={recipe.width}:{recipe.height}")
    args = [
        ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(source),
        "-t", str(recipe.seconds), "-vf", fit,
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        # Оглавление вперёд — то самое требование, ради которого спека и заведена.
        "-movflags", "+faststart",
    ]
    if not recipe.audio:
        args.append("-an")
    return run(args + quality_args(encoder, quality) + [str(target)])


def make_poster(ffmpeg, video, target):
    """Постер — ПЕРВЫЙ КАДР ГОТОВОГО файла, а не исходника и не чужая картинка.

    Он виден те доли секунды, пока видео не подставлено, и в витрине до попадания
    в экран. Возьми другую картинку — на загрузке будет скачок. Машине это не
    проверить (кадр от кадра не отличить), поэтому правило держится здесь.
    """
    return run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(video),
                "-frames:v", "1", "-q:v", "3", str(target)])


def bake(ffmpeg, encoder, source, name, recipe, out):
    """Печёт и возвращает (видео, постер). Кидает SystemExit с текстом причины."""
    out.mkdir(parents=True, exist_ok=True)
    # Рецепт в имени: без него шапка и фон из одного исходника оба звались бы
    # istochnik.mp4, и второй молча затирал бы первый.
    video = out / f"{source.stem}-{name}.mp4"
    poster = out / f"{source.stem}-{name}.jpg"

    for attempt in range(TRIES):
        quality = QUALITY + attempt * STEP
        done = encode(ffmpeg, source, video, recipe, encoder, quality)
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
    """Какие дорожки печь для исходника такой высоты.

    Вверх не растягиваем никогда: из записи 720p дорожка 1080p не появится — выйдет
    тот же кадр крупнее и вдвое тяжелее. Запись ниже нижней ступени берём как есть,
    одной дорожкой: лекция в 480p лучше, чем никакой. Высота чётная — этого требует
    yuv420p, а нечётную даёт, например, кадр 1079 после чужого кропа.
    """
    return [step for step in ladder.heights if step <= height] or [height - height % 2]


def lecture_filter(heights, out_fps, source_fps, denoise):
    """Цепочка фильтров: почистить и разветвить НА ОДНОМ декодировании.

    Разветвление здесь не для красоты. Отдельным запуском на каждую дорожку файл
    декодировался бы и чистился заново, а чистка — самая дорогая часть: на замере
    два прохода стоили ровно вдвое дольше одного с `split`.
    """
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


def encode_ladder(ffmpeg, source, out, ladder, heights, about, encoder, quality, denoise):
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
    # Опорный кадр строго на границе сегмента. Через -g это считалось бы от частоты
    # кадров, а она бывает дробной (29.97) — тогда сегменты поехали бы. Выражение
    # от ВРЕМЕНИ не зависит от частоты вовсе.
    args += ["-force_key_frames", f"expr:gte(t,n_forced*{ladder.segment})"]
    args += quality_args(encoder, quality)
    if audio:
        args += ["-c:a", "aac", "-b:a", f"{ladder.audio_kbps}k", "-ac", "2"]

    args += [
        "-f", "hls", "-hls_time", str(ladder.segment), "-hls_playlist_type", "vod",
        # fMP4, а не привычный TS: тот же кусок годится и для DASH, и весит меньше —
        # у TS свой заголовок в каждом из 188-байтовых пакетов.
        "-hls_segment_type", "fmp4",
        # Каждый сегмент начинается с опорного кадра — без этого плеер не умеет
        # переключать качество на границе, а перемотка попадает мимо.
        "-hls_flags", "independent_segments",
        "-var_stream_map", " ".join(variants), "-master_pl_name", spec.MASTER,
        # as_posix, а не str: имена из этих шаблонов ffmpeg кладёт В МАНИФЕСТ как есть,
        # а там это URL, а не путь по диску. С windows-путём в мастер уезжало
        # «0\index.m3u8», и набор, испечённый на этой машине, не заиграл бы нигде.
        # Заодно от обратной косой ломался вывод init-сегмента — он не создавался совсем.
        "-hls_segment_filename", (out / "%v" / "seg%05d.m4s").as_posix(),
        (out / "%v" / "index.m3u8").as_posix(),
    ]
    return run(args)


def _attributes(line):
    """Разбор `#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="a,b"` — запятые внутри кавычек
    делить нельзя, поэтому вручную, а не split(',')."""
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
    """Что вышло — по манифестам, ровно как это прочтёт сайт.

    Не по списку файлов в папке: сайт увидит набор глазами плеера, и расходиться
    эти два взгляда не должны. Заодно так ловится оборванная выпечка — манифест
    без `#EXT-X-ENDLIST` или короче, чем длительность.
    """
    lines = (out / spec.MASTER).read_text(encoding="utf-8").splitlines()
    renditions, waiting = [], None
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF"):
            waiting = _attributes(line)
        elif waiting is not None and line.strip() and not line.startswith("#"):
            uri = line.strip()
            # Проверяем прямо здесь, а не «оно же очевидно»: пекарня живёт на windows,
            # а играть манифест будут где угодно. Один раз обратная коса сюда уже уехала.
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


def bake_lecture(ffmpeg, encoder, source, name, ladder, out, denoise):
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

    done = encode_ladder(ffmpeg, source, out, ladder, heights, about, encoder, quality, denoise)
    if done.returncode:
        raise SystemExit(f"ffmpeg не справился:\n{done.stderr.strip()}")

    poster = out / spec.POSTER
    # Не первым кадром, в отличие от косметики: там постер — подложка под то же видео
    # и обязан совпадать с ним, а здесь это картинка в списке, и пустая доска в первую
    # секунду записи никому ничего не скажет.
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
    """Проверяем свой результат ТОЙ ЖЕ функцией, которой его встретит сайт. Иначе
    «у меня всё хорошо» и «приняли» были бы двумя разными проверками."""
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
        detail = json.loads(error.read() or b"{}").get("error", "")
        raise SystemExit(f"приёмка ответила {error.code}: {detail or error.reason}")


def fetch_source(url, target):
    """Скачать сырьё прямо из хранилища. Потоком: лекция весит гигабайты, в память
    такое не берут."""
    with urllib.request.urlopen(url, timeout=120) as answer, target.open("wb") as file:
        shutil.copyfileobj(answer, file, length=4 * 1024 * 1024)
    return target.stat().st_size


def put_file(url, path):
    request = urllib.request.Request(url, data=path.read_bytes(), method="PUT")
    with urllib.request.urlopen(request, timeout=120) as answer:
        if answer.status >= 300:
            raise OSError(f"хранилище ответило {answer.status} на {path.name}")


def upload(site, token, job_token, folder):
    """Залить готовый набор напрямую в хранилище.

    Кусков тысячи, поэтому ссылки берём порциями, а сами куски льём по несколько разом:
    каждый маленький, и последовательно они уходили бы часами на одних рукопожатиях.
    """
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    names = [path.relative_to(folder).as_posix() for path in files]
    beside = dict(zip(names, files))
    done = 0
    for start in range(0, len(names), SIGN_BATCH):
        batch = names[start:start + SIGN_BATCH]
        urls = talk(site, token, "/intake/sign/", {"token": job_token, "names": batch})["urls"]
        with ThreadPoolExecutor(max_workers=UPLOAD_THREADS) as pool:
            for _ in pool.map(lambda name: put_file(urls[name], beside[name]), batch):
                done += 1
        say(f"    залито {done} из {len(names)}")
    return len(names)


def serve_once(site, token, ffmpeg, encoder, denoise, known):
    """Взять одно задание и довести до конца. False — работы не было."""
    answer = talk(site, token, "/intake/claim/", {"worker": socket.gethostname()})
    job = answer.get("job")
    if not job:
        return False

    say(f"задание #{job['id']} · {job['recipe']}")
    recipe = known.get(job["recipe"])
    if recipe is None:
        talk(site, token, "/intake/fail/", {"token": job["token"], "error": f"нет рецепта {job['recipe']}"})
        return True

    workshop = Path(tempfile.mkdtemp(prefix="bake-"))
    try:
        source = workshop / (job["name"] or "source")
        say(f"  качаю сырьё…")
        say(f"  сырьё: {human(fetch_source(job['source'], source))}")

        out, made = bake_lecture(ffmpeg, encoder, source, job["recipe"], recipe, workshop / "out", denoise)
        if problem := spec.check_ladder(recipe, made):
            raise SystemExit(problem)

        plan = talk(site, token, "/intake/plan/", {"token": job["token"], "manifest": made})
        say(f"  папка: {plan['prefix']}")
        upload(site, token, job["token"], out)
        talk(site, token, "/intake/commit/", {"token": job["token"]})
        say("  готово")
    except SystemExit as error:
        say(f"  НЕ ВЫШЛО: {error}")
        talk(site, token, "/intake/fail/", {"token": job["token"], "error": str(error)[:300]})
    except Exception as error:  # noqa: BLE001 — что угодно, лишь бы сказать это сайту
        say(f"  СЛОМАЛОСЬ: {error}")
        talk(site, token, "/intake/fail/", {"token": job["token"], "error": f"{type(error).__name__}: {error}"[:300]})
    finally:
        # Сырьё и готовое весят десятки гигабайт — на диске пекарни им не место.
        shutil.rmtree(workshop, ignore_errors=True)
    return True


def serve(args, ffmpeg, encoder, denoise, known, site, token):
    """Ждать заданий и печь их по одному."""
    if not site or not token:
        raise SystemExit("для очереди нужны INTAKE_URL и INTAKE_TOKEN")
    say(f"пекарня ждёт работы: {site}, опрос раз в {args.every} с")
    while True:
        try:
            worked = serve_once(site, token, ffmpeg, encoder, denoise, known)
        except SystemExit as error:  # приёмка недоступна — не повод умирать
            say(f"  приёмка молчит: {error}")
            worked = False
        if args.once:
            return 0
        time.sleep(1 if worked else args.every)


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
    encoder = "libx264" if args.cpu or not has_nvenc(ffmpeg) else NVENC
    denoise = "" if args.denoise.lower() in ("none", "нет", "") else args.denoise

    if args.serve or args.once:
        return serve(args, ffmpeg, encoder, denoise, known, site, token)
    if not args.kind or not args.source:
        parser.error("нужны вид и исходник — или --serve, чтобы брать задания из очереди")
    if not args.source.exists():
        raise SystemExit(f"нет такого файла: {args.source}")
    name, recipe = pick(known, args.kind)

    say(f"пекарня · {recipe.title} ({name})")
    say(f"  требования: {whence}")
    say(f"  кодировщик: {encoder}{' — на видеокарте' if encoder == NVENC else ''}")
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
    sys.exit(main())
