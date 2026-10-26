"""Episodes for kumapie made on the PC instead of the phone (config.toml [aniworld]): whatever the phone's Add an
episode sends with "Make it on: the PC", and AniWorld / s.to links (always here).

  PUT  /api/aniworld/upload/<id>/<name>?offset=N&total=T   a video file from the phone, resumable: answers {"size"};
                       409 with {"size"} when the PC has another size (the phone goes on from there)
  POST /api/aniworld   {person, lang, url | upload, kind?, count?, height?, show?, type?, rd?, jimaku?}
                       person = a [sync.people] key (the device's owner), lang = the episode's language;
                       url = an aniworld.to / s.to link (lang de/en: the dub), a YouTube or TikTok link (kind = video /
                       shorts / videos / playlist: this video, the newest [count] of its channel's shorts or videos,
                       the playlist), or a magnet / hoster / Real-Debrid link (rd = the phone's Real-Debrid token);
                       upload = "<id>/<name>" sent first with PUT; show = the show's name (blank: the file's, the
                       channel's, AniList's); type = anime / shows / movies (the phone's Find);
                       jimaku = the phone's Jimaku key: Japanese subtitles from Jimaku, fitted with alass.
                       Queued, one job at a time.
  GET  /api/aniworld   the jobs since the server started, newest first (without keys and tokens)

Each video ends up as library\\<key>\\<kind>\\<show>\\<stem>.mp4 + <stem>.srt (+ <stem>.<translation>.srt), then
pc_episodes.py makes the episode and tv.py's rescan lists it within a minute; the phones get it on Wi-Fi.
The words on screen: the video's own subtitles (MKV tracks, YouTube's human ones), else Jimaku's (Japanese), else
Parakeet on the CPU (pipeline\\parakeet.py; Japanese: Parakeet ja). The line under them only from subtitles.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from dojo import cfg

sys.path.insert(0, str(cfg.kumapie_app / "mobile" / "src" / "main" / "python"))  # the phone's own modules
import jimaku  # noqa: E402
import mkvsubs  # noqa: E402
import realdebrid  # noqa: E402

CONF = cfg.raw["aniworld"]
VENV = cfg.root / CONF["venv"] / "Scripts"
ALASS = cfg.root / CONF["alass"]
PARAKEET_JA = cfg.root / CONF["parakeet_ja"]
PEOPLE: dict[str, dict[str, str]] = cfg.raw.get("sync", {}).get("people", {})
ANIWORLD = re.compile(r"^https://(aniworld\.to/anime|s\.to/serie|serienstream\.to/serie)/stream/[\w-]+"
                      r"(/staffel-\d+(/episode-\d+)?|/filme(/film-\d+)?)?/?$")
YOUTUBE = re.compile(r"^https://((www|m)\.)?(youtube\.com|youtu\.be)/[\w\-/?=&.@%]+$")
TIKTOK = re.compile(r"^https://((www|vm|vt|m)\.)?tiktok\.com/[\w\-/?=&.@%]+$")
TIKTOK_ACCOUNT = re.compile(r"^https://((www|m)\.)?tiktok\.com/@[\w.\-]+/?(\?[\w=&.%-]*)?$")  # its newest [count]
CHANNEL = re.compile(r"^https://((www|m)\.)?youtube\.com/(@[^/?]+|channel/[^/?]+|c/[^/?]+|user/[^/?]+)")
LINK = re.compile(r"^(magnet:\?\S+|https://\S+)$")  # Real-Debrid: a magnet or a hoster / Real-Debrid link
UPLOAD_ID = re.compile(r"^[0-9a-f]{8,40}$")
KINDS = ("video", "shorts", "videos", "playlist")
TYPES = ("anime", "shows", "movies")
EPISODE = re.compile(r"S(\d+)E(\d+)", re.I)
AUDIO = {"ja": ("jpn", "ja"), "de": ("ger", "deu", "de"), "en": ("eng", "en")}  # an audio track's language tag
LOG = cfg.work / "logs" / "aniworld.log"
WORK = cfg.work / "pcjobs"
UPLOADS = WORK / "uploads"
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = str(Path(FFMPEG).with_name("ffprobe" + Path(FFMPEG).suffix)) if os.path.dirname(FFMPEG) else "ffprobe"
ENV = {**os.environ, "PYTHONUTF8": "1", "HF_HOME": str(cfg.root / CONF["hf_home"]),
       "PATH": os.path.dirname(FFMPEG) + os.pathsep + os.environ.get("PATH", ""),  # aniworld and parakeet.py run ffmpeg
       "ALASS_FFMPEG_PATH": FFMPEG, "ALASS_FFPROBE_PATH": FFPROBE}  # alass reads a video reference with them
SECRETS = ("key", "rd", "jimaku")
SAVED = cfg.state / "feed" / "jobs.json"  # the unfinished jobs (tokens too): queued again when the server starts

jobs: list[dict] = []
_queue: queue.Queue = queue.Queue()
_saving = threading.Lock()


def _save() -> None:
    """The unfinished jobs to disk, so a restart (new code, a crash) goes on with them."""
    with _saving:
        part = SAVED.with_name(SAVED.name + ".part")
        part.write_text(json.dumps([j for j in jobs if j["state"] in ("queued", "running")]), encoding="utf-8")
        part.replace(SAVED)


def _again(saved: list[dict]) -> list[dict]:
    """Saved jobs (newest first) -> oldest first, queued again. Each kind skips what it already made (AniWorld goes on
    in its kept work folder). One cut off 3 times fails instead: it may be what brings the server down."""
    for job in saved:
        job["restarts"] = job.get("restarts", 0) + 1
        if job["restarts"] > 3:
            job.update(state="failed", error="stopped by 3 restarts of the server")
        else:
            job.update(state="queued", step="queued again after a restart")
    return saved[::-1]


def _kill_on_exit():
    """Windows: a job object that ends the processes put in it when this server ends, however it ends (new code:
    os._exit(3)). Without it aniworld.exe, ffmpeg and Parakeet ran on as LocalSystem after a restart, and only an
    Administrator could end them (two aniworld.exe on 2026-10-24). Answers what puts a process in it."""
    if os.name != "nt":
        return lambda p: None
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [("user_time", ctypes.c_int64 * 2), ("flags", wintypes.DWORD), ("working_set", ctypes.c_size_t * 2),
                    ("processes", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD * 2)]

    class Extended(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6), ("memory", ctypes.c_size_t * 4)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: the handle closes with this process
    job = k32.CreateJobObjectW(None, None)
    if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = extended
        return lambda p: None
    return lambda p: k32.AssignProcessToJobObject(job, int(p._handle))


_contain = _kill_on_exit()


def submit(body: dict) -> dict:
    person, lang = str(body.get("person", "")), str(body.get("lang", ""))
    url, upload = str(body.get("url") or "").strip(), str(body.get("upload") or "")
    key = PEOPLE.get(person, {}).get(lang)
    if not key:
        raise ValueError(f"no {lang!r} library for {person!r}")
    if upload:
        uid, _, name = upload.partition("/")
        if not UPLOAD_ID.match(uid) or not (UPLOADS / uid / _safe(name)).exists():
            raise ValueError("upload not found")
        source = "file"
    elif ANIWORLD.match(url):
        if lang not in CONF["dub"]:
            raise ValueError(f"no {lang!r} dub on AniWorld")
        source = "aniworld"
    elif YOUTUBE.match(url) or TIKTOK.match(url):
        source = "youtube"
    elif LINK.match(url):
        if not body.get("rd"):
            raise ValueError("a Real-Debrid link needs the Real-Debrid token")
        source = "rd"
    else:
        raise ValueError("not a link the PC can make an episode of")
    kind = str(body.get("kind") or "video") if source == "youtube" else ""
    if source == "youtube" and kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    job = {"id": uuid.uuid4().hex[:8], "person": person, "lang": lang, "url": url or upload.partition("/")[2],
           "upload": upload, "source": source, "kind": kind, "key": key,
           "count": max(1, min(int(body.get("count") or 10), 200)), "height": int(body.get("height") or 720),
           "show": str(body.get("show") or "").strip(), "type": str(body.get("type") or "") if body.get("type") in TYPES else "",
           "rd": str(body.get("rd") or ""), "jimaku": str(body.get("jimaku") or ""),
           # Which episode one file is (stremio.py, from the Stremio id): names it instead of the file name's guess.
           "season": int(body.get("season") or 0), "number": int(body.get("number") or 0),
           "state": "queued", "step": "", "error": "", "episodes": []}
    jobs.insert(0, job)
    _queue.put(job)
    _save()
    return listing()[0]


def receive(uid: str, name: str, offset: int, total: int, read, length: int) -> int:
    """A piece of an upload (PUT): appended at [offset], which must be the size the PC has (else ValueError(size));
    at [total] bytes <name>.part becomes <name>. Answers the size the PC has."""
    if not UPLOAD_ID.match(uid) or not _safe(name):
        raise ValueError(0)
    done = UPLOADS / uid / _safe(name)
    if done.exists():
        return done.stat().st_size
    part = done.with_name(done.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if offset != have:
        raise ValueError(have)
    with open(part, "ab") as f:
        while length > 0 and (chunk := read(min(length, 1 << 20))):
            f.write(chunk)
            length -= len(chunk)
            have += len(chunk)
    if have == total:
        part.replace(done)
    return have


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")[:200]


def _start(cmd, **kw) -> subprocess.Popen:
    p = subprocess.Popen([str(c) for c in cmd], env=ENV, stdin=subprocess.DEVNULL, **kw)
    _contain(p)  # ends with the server; what it starts later (aniworld.exe's ffmpeg) is in the job too
    return p


def _run(cmd, check=True, **kw) -> None:
    with open(LOG, "a", encoding="utf-8") as log:
        log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(map(str, cmd))}\n")
        log.flush()
        code = _start(cmd, stdout=log, stderr=subprocess.STDOUT, **kw).wait()
    if check and code:
        raise subprocess.CalledProcessError(code, cmd)


def _out(cmd, both=False) -> str:
    """A command's output (its messages go to the log; [both]: they are part of the answer too)."""
    with open(LOG, "a", encoding="utf-8") as log:
        log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(map(str, cmd))}\n")
        log.flush()
        p = _start(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if both else log, text=True,
                   encoding="utf-8", errors="replace")
        out = p.communicate()[0]
        if p.returncode:
            raise subprocess.CalledProcessError(p.returncode, cmd, out)
        if both:
            log.write(out)
        return out


def _ascii(text: str) -> str:
    """'Café Trip! Vlog #3' -> 'cafe_trip_vlog_3' (a Japanese title: '')."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", plain).strip("_")


def _slug(folder: str) -> str:
    """'You and I Are Polar Opposites (2026-2026) [imdbid-tt37532731]' -> 'you_and_i_are_polar_opposites'."""
    return _ascii(re.sub(r"\s*[(\[].*$", "", folder))


def _srt(cues: list, path: Path) -> None:
    def ts(t):
        ms = round(t * 1000)
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    path.write_text("".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(cues, 1)), encoding="utf-8")


class _Log:
    """realdebrid.py's progress lines -> the job's step."""
    def __init__(self, job, prefix=""):
        self.job, self.prefix = job, prefix

    def log(self, line):
        self.job["step"] = self.prefix + line


def _transcribe(folder: Path, stem: str, lang: str) -> None:
    """<stem>.mp4 -> <stem>.srt with Parakeet (Japanese: Parakeet ja)."""
    if lang == "ja":
        _run([VENV / "python.exe", cfg.pipeline / "parakeet.py", "--ja", PARAKEET_JA, "-o", stem, f"{stem}.mp4"], cwd=folder)
    else:
        _run([VENV / "python.exe", cfg.pipeline / "parakeet.py", "-o", stem, f"{stem}.mp4"], cwd=folder)
        _run([cfg.python, cfg.pipeline / "srt_spaced.py", "-o", stem, f"{stem}.json"], cwd=folder)


def _episodes(job: dict, made: list[Path]) -> None:
    if made:
        job["step"] = "building episodes"
        # Only this job's videos: the whole library would also list episodes that live only on a device (duplicates).
        _run([cfg.python, cfg.code / "tools" / "kumapie" / "pc_episodes.py", "--for", job["key"], *made])


# ---------------------------------------------------------------- YouTube / TikTok

def _yt() -> list:
    # The service has no user PATH: Node (system-wide) solves YouTube's player JavaScript, ffmpeg joins video and sound.
    node = shutil.which("node", path=os.environ.get("PATH", "") + os.pathsep + r"C:\Program Files\nodejs")
    return [VENV / "yt-dlp.exe", "--no-progress", "--ffmpeg-location", FFMPEG] + (["--js-runtimes", f"node:{node}"] if node else [])


def _videos(job: dict) -> list[str]:
    """The job's video links: the link itself, or the newest [count] of its channel's shorts / videos, or the playlist."""
    url, kind = job["url"], job["kind"]
    if TIKTOK_ACCOUNT.match(url):  # followed on a phone, which can't list TikTok: the PC lists it, newest first
        urls = _out(_yt() + ["--flat-playlist", "-I", f"1:{job['count']}", "--print", "url", url]).split()
        if not urls:
            raise RuntimeError(f"no videos at {url}")
        return urls
    if kind == "video":
        return [url]
    if kind != "playlist":
        m = CHANNEL.match(url)
        channel = m.group(0) if m else _out(_yt() + ["--skip-download", "--no-playlist", "--print", "channel_url", url]).strip()
        url = channel.rstrip("/") + ("/shorts" if kind == "shorts" else "/videos")
    ids = _out(_yt() + ["--flat-playlist", "-I", f"1:{job['count']}", "--print", "id", url]).split()
    if not ids:
        raise RuntimeError(f"no {kind} at {url}")
    return [f"https://www.youtube.com/shorts/{i}" if kind == "shorts" else f"https://www.youtube.com/watch?v={i}" for i in ids]


def _youtube(job: dict) -> None:
    lang, translation = job["lang"], _translation(job["key"])
    job["step"] = "asking YouTube"
    urls = _videos(job)
    made, failed = [], []
    for i, url in enumerate(urls, 1):
        try:
            info = json.loads(_out(_yt() + ["-J", "--no-playlist", url]))
            # Japanese sound under German subtitles was made into "German" episodes (Yamanobori 0, 2026-10-26).
            heard = {(f.get("language") or "").split("-")[0] for f in info.get("formats") or [] if f.get("acodec") not in (None, "none")}
            if heard - {""} and lang not in heard:
                raise RuntimeError(f"no {lang} sound (only {', '.join(sorted(heard - {''}))})")
            short =job["kind"] == "shorts" or "/shorts/" in url or bool(TIKTOK.match(url))
            show = (_ascii(job["show"]) or _ascii(info.get("channel") or info.get("uploader") or "")
                    or _ascii(info.get("uploader_id") or "") or "youtube")
            folder = cfg.library(job["key"]) / ("reels" if short else "youtube") / show
            stem = "_".join(filter(None, [_ascii(info.get("title") or "")[:50].strip("_"), str(info["id"])]))
            if (folder / f"{stem}.kumapie.json").exists():
                continue
            folder.mkdir(parents=True, exist_ok=True)
            if not (folder / f"{stem}.mp4").exists():
                job["step"] = f"{i}/{len(urls)} {stem}: downloading"
                h = job["height"]
                _run(_yt() + ["--no-playlist", "-f", f"bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/bv*[ext=mp4][height<={h}]+ba[ext=m4a]"
                              f"/b[vcodec^=h264]/b[ext=mp4]/b", "--merge-output-format", "mp4", "--write-subs",
                              "--sub-langs", f"{lang}.*,{translation}.*", "--convert-subs", "srt",
                              "-o", f"{stem}.%(ext)s", url], cwd=folder)
                (folder / f"{stem}.title.txt").write_text(info.get("title") or stem, encoding="utf-8")
                # The video's own subtitles (made by people) -> <stem>.srt and <stem>.<translation>.srt, as rd.py's.
                for sub in sorted(folder.glob(f"{stem}.*.srt"), key=lambda f: len(f.name)):
                    code = sub.name[len(stem) + 1:-4].split("-")[0]
                    target = folder / (f"{stem}.srt" if code == lang else f"{stem}.{translation}.srt")
                    if code in (lang, translation) and not target.exists():
                        sub.replace(target)
                    elif sub != target:
                        sub.unlink()
            if not (folder / f"{stem}.srt").exists():
                job["step"] = f"{i}/{len(urls)} {stem}: transcribing (Parakeet)"
                _transcribe(folder, stem, lang)
            made.append(folder / f"{stem}.mp4")
            job["episodes"].append(stem)
        except Exception as e:  # noqa: BLE001 - one bad video doesn't stop the channel's others
            failed.append(f"{url}: {str(e)[:100]}")
    _episodes(job, made)
    if failed:
        raise RuntimeError(f"{len(failed)} of {len(urls)} failed: " + "; ".join(failed))


# ---------------------------------------------------------------- a video file (Real-Debrid, uploaded)

def _translation(key: str) -> str:
    person = cfg.people.get(key)
    return person.translation if person else "en"


def _remux(src: Path, dst: Path, lang: str) -> None:
    """Any video -> MP4 for the phones: the episode language's audio as AAC; the video copied, or a heavy one (over
    15 Mbps, a BluRay remux) re-encoded to HEVC 5 Mbps, as the phone does. Not H.264/HEVC (AV1: the bedroom TV's
    MediaTek decoder shows one frame and stalls) -> H.264."""
    probe = json.loads(_out([FFPROBE, "-v", "error", "-show_entries", "stream=index,codec_type,codec_name:stream_tags=language:format=bit_rate",
                             "-of", "json", src]))
    audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    if not audio:
        raise RuntimeError(f"no sound in {src.name}")
    track = next((s for s in audio if (s.get("tags") or {}).get("language", "").lower() in AUDIO.get(lang, (lang,))), audio[0])
    heavy = int(probe.get("format", {}).get("bit_rate") or 0) > 15_000_000
    codec = next((s.get("codec_name") for s in probe["streams"] if s["codec_type"] == "video"), "")
    video = ["-c:v", "libx265", "-preset", "fast", "-b:v", "5M", "-tag:v", "hvc1"] if heavy else \
        ["-c:v", "copy"] if codec in ("h264", "hevc") else ["-c:v", "libx264", "-preset", "faster", "-crf", "23", "-pix_fmt", "yuv420p"]
    part = dst.with_name(dst.stem + ".part.mp4")
    _run([FFMPEG, "-v", "error", "-y", "-i", src, "-map", "0:v:0", "-map", f"0:{track['index']}", *video,
          "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-movflags", "+faststart", part])
    part.replace(dst)


def _strays(log: str) -> bool:
    """alass moved a part of 5+ lines over 20 s away from the biggest part (an opening song taken for speech): the
    phone's SubSync.strayPart."""
    parts = [(int(n), (-1 if sign else 1) * (int(h) * 3600 + int(m) * 60 + float(s)))
             for n, sign, h, m, s in re.findall(r"shifted block of (\d+) subtitles .*? by (-?)(\d+):(\d+):(\d+\.\d+)", log)]
    if not parts:
        return False
    main = max(parts)[1]
    return any(n >= 5 and abs(shift - main) > 20 for n, shift in parts)


def _jimaku(job: dict, video: Path, show: str, season, number, name: str, tmp: Path, english: Path | None):
    """Japanese subtitles from Jimaku fitted to [video] with alass -> (cues, AniList entry), or (None, entry)."""
    found = json.loads(jimaku.candidates(job["jimaku"], show, season, number, name))
    if not found["files"]:
        return None, found.get("anilist")
    pick = found["files"][0]
    raw = Path(jimaku.download(job["jimaku"], pick["url"], pick["name"], number, str(tmp)))
    # The video's own English subtitles, else the video itself (alass finds its speech). Silero's speech spans, the
    # phone's reference, put Lucky Star 01 45 s off (its opening song); these two both got it right to 0.03 s.
    ref = english or video
    fitted = tmp / f"fitted{raw.suffix.lower()}"
    log = _out([ALASS, ref, raw, fitted, "--split-penalty", "11"], both=True)
    if _strays(log):
        _out([ALASS, ref, raw, fitted, "--no-split"], both=True)
    return json.loads(jimaku.cues(str(fitted), "ja")), found.get("anilist")


def _local(job: dict, src: Path, name: str, single: bool, tmp: Path, prefix: str = "") -> Path:
    """A video file (from Real-Debrid or the phone) -> its place in the library with subtitles; answers the MP4."""
    lang, translation = job["lang"], _translation(job["key"])
    about = json.loads(realdebrid.describe(name, single))
    show, season, number = job["show"] or about["show"] or Path(name).stem, about["season"], about["number"]
    if job["number"]:  # stremio.py knows which episode it is: not the file name's guess
        season, number = job["season"] or None, job["number"]
    # Subtitles inside an MKV are made for this very video.
    subs = {}
    if src.suffix.lower() == ".mkv":
        for code in {lang, translation, "en"}:
            got = json.loads(mkvsubs.extract(str(src), code, str(tmp)))
            if got:
                subs[code] = Path(got["path"])
    cues, anilist = None, None
    if lang in subs:
        cues = json.loads(jimaku.cues(str(subs[lang]), lang))
    elif lang == "ja" and job["jimaku"]:
        job["step"] = prefix + "Japanese subtitles from Jimaku"
        cues, anilist = _jimaku(job, src, show, season, number, name, tmp, subs.get("en"))
    if lang == "ja" and anilist and not job["show"]:
        show = anilist.get("romaji") or show
    kind = job["type"] or ("anime" if lang == "ja" else "shows" if number else "movies")
    slug = _ascii(show) or _ascii(Path(name).stem) or "video"
    folder = cfg.library(job["key"]) / kind / slug
    stem = f"{slug}_s{season or 1}_{number:02}" if number else slug
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{stem}.mp4"
    if not video.exists():
        job["step"] = prefix + f"{stem}: converting"
        _remux(src, video, lang)
    if cues and not (folder / f"{stem}.srt").exists():
        _srt(cues, folder / f"{stem}.srt")
    if translation != lang and translation in subs and not (folder / f"{stem}.{translation}.srt").exists():
        _srt(json.loads(jimaku.cues(str(subs[translation]), translation)), folder / f"{stem}.{translation}.srt")
    if not (folder / f"{stem}.srt").exists():
        job["step"] = prefix + f"{stem}: transcribing (Parakeet)"
        _transcribe(folder, stem, lang)
    job["episodes"].append(stem)
    return video


def _rd(job: dict, tmp: Path) -> None:
    job["step"] = "asking Real-Debrid"
    files = json.loads(realdebrid.episodes(job["rd"], job["url"], _Log(job)))
    failed = []
    for i, f in enumerate(files, 1):
        prefix = f"{i}/{len(files)} "
        try:
            # An episode named by stremio.py (show, season, number) that's already in the library isn't downloaded again.
            if job["show"] and job["number"]:
                kind = job["type"] or ("anime" if job["lang"] == "ja" else "shows")
                slug = _ascii(job["show"])
                stem = f"{slug}_s{job['season'] or 1}_{job['number']:02}"
                if (cfg.library(job["key"]) / kind / slug / f"{stem}.kumapie.json").exists():
                    job["episodes"].append(stem)
                    continue
            path = Path(realdebrid.download(job["rd"], f["link"], str(tmp / "download"), _Log(job, prefix)))
            _episodes(job, [_local(job, path, f["name"], f["single"], tmp, prefix)])  # each one as soon as it's made
            path.unlink()
        except Exception as e:  # noqa: BLE001 - one bad file doesn't stop a season's others
            failed.append(f"{f['name']}: {str(e)[:100]}")
    if failed:
        raise RuntimeError(f"{len(failed)} of {len(files)} failed: " + "; ".join(failed))


def _file(job: dict, tmp: Path) -> None:
    uid, _, name = job["upload"].partition("/")
    src = UPLOADS / uid / _safe(name)
    _episodes(job, [_local(job, src, name, True, tmp)])
    shutil.rmtree(UPLOADS / uid, ignore_errors=True)


# ---------------------------------------------------------------- AniWorld

def _seasons(url: str) -> list[str]:
    """A show's season links (staffel-1, staffel-2...) from its page: the CLI given the show's own link waits forever."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        page = r.read().decode("utf-8", "replace")
    path = re.escape(urllib.parse.urlsplit(url).path.rstrip("/"))
    found = {int(n) for n in re.findall(rf'href="{path}/staffel-(\d+)"', page)}
    return [f"{url.rstrip('/')}/staffel-{n}" for n in sorted(found)]


def _aniworld(job: dict, out: Path) -> None:
    show_page = not re.search(r"/(staffel-\d+(/episode-\d+)?|filme(/film-\d+)?)/?$", job["url"])
    links = _seasons(job["url"]) if show_page else [job["url"]]
    if not links:
        raise RuntimeError("no seasons on the show's page")
    total = 0
    # One season at a time, made and listed before the next downloads: a long show isn't all on disk at once, and a
    # later season failing keeps the earlier ones.
    for n, link in enumerate(links, 1):
        job["step"] = f"downloading season {n} of {len(links)}" if len(links) > 1 else "downloading"
        # The CLI skips what it has: the second run retries only failed episodes (a host's hiccup). Its exit code isn't
        # checked: a failed single episode exits 1, a season with failed episodes exits 0; what's on disk is what counts.
        for _ in range(2):
            _run([VENV / "aniworld.exe", "-a", "Download", "-l", CONF["dub"][job["lang"]], "-o", out, link], check=False)
        made = _made_aniworld(job, out)
        _episodes(job, made)
        total += len(made)
    if not total:
        raise RuntimeError(f"no {CONF['dub'][job['lang']]} for this link (or the hosts failed twice)")


def _made_aniworld(job: dict, out: Path) -> list[Path]:
    """The downloaded episodes in [out] -> library MP4 + transcript; the downloads are deleted once made."""
    videos = sorted(f for f in out.rglob("*") if f.suffix.lower() in (".mkv", ".mp4") and EPISODE.search(f.name)
                    and ".temp" not in f.name)  # a failed download leaves <name>.temp_full.mkv
    made = []
    for i, src in enumerate(videos, 1):
        season, number = (int(n) for n in EPISODE.search(src.name).groups())
        slug = _slug(src.relative_to(out).parts[0])
        folder = cfg.library(job["key"]) / "anime" / slug
        stem = f"{slug}_s{season}_{number:02}"
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / f"{stem}.mp4").exists():
            job["step"] = f"{i}/{len(videos)} {stem}: remux"
            tmp = folder / f"{stem}.part.mp4"
            _run([FFMPEG, "-v", "error", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                  "-movflags", "+faststart", tmp])
            tmp.replace(folder / f"{stem}.mp4")
        if not (folder / f"{stem}.srt").exists():
            job["step"] = f"{i}/{len(videos)} {stem}: transcribing (Parakeet)"
            _transcribe(folder, stem, job["lang"])
        made.append(folder / f"{stem}.mp4")
        job["episodes"].append(stem)
        src.unlink()
    return made


# ----------------------------------------------------------------

def _process(job: dict) -> None:
    tmp = WORK / job["id"]
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        {"youtube": lambda: _youtube(job), "rd": lambda: _rd(job, tmp), "file": lambda: _file(job, tmp),
         "aniworld": lambda: _aniworld(job, tmp)}[job["source"]]()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _worker() -> None:
    while True:
        job = _queue.get()
        job["state"] = "running"
        try:
            _process(job)
            job["state"], job["step"] = "done", ""
        except Exception as e:  # noqa: BLE001 - reported to the device
            job["state"], job["error"] = "failed", str(e)[:300]
        _save()


def listing() -> list[dict]:
    return [{k: v for k, v in j.items() if k not in SECRETS} for j in jobs]


if __name__ != "__main__":  # the server: jobs a restart cut off go on (the self-check below must not run them)
    try:
        for _job in _again(json.loads(SAVED.read_text(encoding="utf-8"))):
            jobs.insert(0, _job)
            if _job["state"] == "queued":
                _queue.put(_job)
        _save()  # with the restart counts
    except (OSError, ValueError):
        pass
threading.Thread(target=_worker, daemon=True).start()


if __name__ == "__main__":
    assert ANIWORLD.match("https://aniworld.to/anime/stream/you-and-i-are-polar-opposites/staffel-2/episode-1")
    assert ANIWORLD.match("https://s.to/serie/stream/the-office/staffel-1")
    assert not ANIWORLD.match("https://aniworld.to/anime/stream/x/staffel-1; rm -rf /")
    assert _slug("You and I Are Polar Opposites (2026-2026) [imdbid-tt37532731]") == "you_and_i_are_polar_opposites"
    assert YOUTUBE.match("https://www.youtube.com/shorts/Cn9o7nr_HFo") and YOUTUBE.match("https://youtu.be/abc?si=x")
    assert not YOUTUBE.match("https://www.youtube.com/watch?v=x; rm -rf /")
    assert TIKTOK.match("https://www.tiktok.com/@tagesschau/video/7350000000000000000")
    assert TIKTOK_ACCOUNT.match("https://www.tiktok.com/@tagesschau") and TIKTOK_ACCOUNT.match("https://www.tiktok.com/@ard.de/?lang=de")
    assert not TIKTOK_ACCOUNT.match("https://www.tiktok.com/@tagesschau/video/7350000000000000000")
    assert not TIKTOK_ACCOUNT.match("https://www.tiktok.com/@a; rm -rf /")
    show = lambda u: not re.search(r"/(staffel-\d+(/episode-\d+)?|filme(/film-\d+)?)/?$", u)  # noqa: E731 - as in _aniworld
    assert show("https://aniworld.to/anime/stream/filmeerzaehler") and show("https://aniworld.to/anime/stream/x/")
    assert not show("https://aniworld.to/anime/stream/x/staffel-2/episode-1") and not show("https://aniworld.to/anime/stream/x/staffel-2") and not show("https://aniworld.to/anime/stream/x/filme/film-1")
    assert CHANNEL.match("https://www.youtube.com/@marichannel/shorts").group(0) == "https://www.youtube.com/@marichannel"
    assert _ascii("Café Trip! Vlog #3") == "cafe_trip_vlog_3" and _ascii("コアラの指紋") == ""
    assert _safe("../a:b.mkv") == "_a_b.mkv" and not UPLOAD_ID.match("../x")
    log = ("shifted block of 300 subtitles with length 0:20:00.000 by -0:00:01.000\n"
           "shifted block of 12 subtitles with length 0:01:30.000 by 0:00:45.000\n")
    assert _strays(log) and not _strays(log.splitlines()[0])
    again = _again([{"id": "new", "state": "queued"}, {"id": "old", "state": "running", "restarts": 3}])
    assert [j["id"] for j in again] == ["old", "new"] and [j["state"] for j in again] == ["failed", "queued"]
    p = _start([sys.executable, "-c", "import time; time.sleep(30)"])
    assert p.poll() is None
    assert os.name != "nt" or _contain(p)  # the kill-on-exit job was made and takes the child (again: still true)
    p.kill()
    print("ok")
