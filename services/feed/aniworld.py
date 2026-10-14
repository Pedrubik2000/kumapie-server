"""Episodes for kumapie made on the PC instead of the phone (config.toml [aniworld]): AniWorld / s.to dubs, and YouTube
links when the phone's Add an episode says "Make it on: the PC".

  POST /api/aniworld   {person, lang, url, kind?, count?, height?}: person = a [sync.people] key (the device's owner),
                       lang = the episode's language, url = an aniworld.to or s.to show, season or episode link (lang
                       de/en: the dub), or a YouTube link; kind (YouTube) = video / shorts / videos / playlist: this
                       video, the newest [count] shorts or videos of its channel, the playlist. One job at a time.
  GET  /api/aniworld   the jobs since the server started, newest first: [{id, person, lang, url, kind, state, step,
                       error, episodes}]

A job: the aniworld CLI downloads the dub -> remux to library\\<key>\\anime\\<show>\\<show>_sN_EE.mp4 -> Parakeet
(pipeline\\parakeet.py) -> srt_spaced.py -> pc_episodes.py. tv.py's rescan lists it within a minute.
A YouTube video: yt-dlp (H.264 MP4 + the video's own human subtitles) -> library\\<key>\\youtube\\<channel>\\ (Shorts:
reels\\, feed only) -> its subtitles, else Parakeet (Japanese: Parakeet ja, [aniworld] parakeet_ja) -> pc_episodes.py.
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import json
import time
import unicodedata
import uuid

from dojo import cfg
from dojo.config import safe_name

CONF = cfg.raw["aniworld"]
VENV = cfg.root / CONF["venv"] / "Scripts"
PEOPLE: dict[str, dict[str, str]] = cfg.raw.get("sync", {}).get("people", {})
URL = re.compile(r"^https://(aniworld\.to/anime|s\.to/serie|serienstream\.to/serie)/stream/[\w-]+"
                 r"(/staffel-\d+(/episode-\d+)?|/filme(/film-\d+)?)?/?$")
YOUTUBE = re.compile(r"^https://((www|m)\.)?(youtube\.com|youtu\.be)/[\w\-/?=&.@%]+$")
CHANNEL = re.compile(r"^https://((www|m)\.)?youtube\.com/(@[^/?]+|channel/[^/?]+|c/[^/?]+|user/[^/?]+)")
KINDS = ("video", "shorts", "videos", "playlist")
EPISODE = re.compile(r"S(\d+)E(\d+)", re.I)
LOG = cfg.work / "logs" / "aniworld.log"
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
ENV = {**os.environ, "PYTHONUTF8": "1", "HF_HOME": str(cfg.root / CONF["hf_home"]),
       "PATH": os.path.dirname(FFMPEG) + os.pathsep + os.environ.get("PATH", "")}  # aniworld and parakeet.py run ffmpeg

jobs: list[dict] = []
_queue: queue.Queue = queue.Queue()


def submit(body: dict) -> dict:
    person, lang, url = str(body.get("person", "")), str(body.get("lang", "")), str(body.get("url", "")).strip()
    youtube = bool(YOUTUBE.match(url))
    if not youtube and not URL.match(url):
        raise ValueError("not an aniworld.to / s.to or YouTube link")
    key = PEOPLE.get(person, {}).get(lang)
    if not key or (not youtube and lang not in CONF["dub"]):
        raise ValueError(f"no {lang!r} library for {person!r}")
    kind = str(body.get("kind") or "video") if youtube else ""
    if youtube and kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    job = {"id": uuid.uuid4().hex[:8], "person": person, "lang": lang, "url": url, "key": key, "kind": kind,
           "count": max(1, min(int(body.get("count") or 10), 200)), "height": int(body.get("height") or 720),
           "state": "queued", "step": "", "error": "", "episodes": []}
    jobs.insert(0, job)
    _queue.put(job)
    return listing()[0]


def _run(cmd, check=True, **kw) -> None:
    with open(LOG, "a", encoding="utf-8") as log:
        log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(map(str, cmd))}\n")
        log.flush()
        subprocess.run([str(c) for c in cmd], env=ENV, stdout=log, stderr=subprocess.STDOUT, check=check,
                       stdin=subprocess.DEVNULL, **kw)


def _out(cmd) -> str:
    """A command's output (its messages go to the log)."""
    with open(LOG, "a", encoding="utf-8") as log:
        log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(map(str, cmd))}\n")
        log.flush()
        return subprocess.run([str(c) for c in cmd], env=ENV, stdout=subprocess.PIPE, stderr=log, check=True,
                              stdin=subprocess.DEVNULL, text=True, encoding="utf-8").stdout


def _ascii(text: str) -> str:
    """'Café Trip! Vlog #3' -> 'cafe_trip_vlog_3' (a Japanese title: '')."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", plain).strip("_")


def _slug(folder: str) -> str:
    """'You and I Are Polar Opposites (2026-2026) [imdbid-tt37532731]' -> 'you_and_i_are_polar_opposites'."""
    name = re.sub(r"\s*[(\[].*$", "", folder)
    return re.sub(r"[^\w]+", "_", safe_name(name).lower()).strip("_")


def _yt() -> list:
    # The service has no user PATH: Node (system-wide) solves YouTube's player JavaScript, ffmpeg joins video and sound.
    node = shutil.which("node", path=os.environ.get("PATH", "") + os.pathsep + r"C:\Program Files\nodejs")
    return [VENV / "yt-dlp.exe", "--no-progress", "--ffmpeg-location", FFMPEG] + (["--js-runtimes", f"node:{node}"] if node else [])


def _videos(job: dict) -> list[str]:
    """The job's video links: the link itself, or the newest [count] of its channel's shorts / videos, or the playlist."""
    url, kind = job["url"], job["kind"]
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
    lang = job["lang"]
    person = cfg.people.get(job["key"])
    translation = person.translation if person else "en"
    job["step"] = "asking YouTube"
    urls = _videos(job)
    made, failed = [], []
    for i, url in enumerate(urls, 1):
        try:
            info = json.loads(_out(_yt() + ["-J", "--no-playlist", url]))
            short = job["kind"] == "shorts" or "/shorts/" in url
            channel = _ascii(info.get("channel") or "") or _ascii(info.get("uploader_id") or "") or "youtube"
            folder = cfg.library(job["key"]) / ("reels" if short else "youtube") / channel
            stem = "_".join(filter(None, [_ascii(info.get("title") or "")[:50].strip("_"), info["id"]]))
            if (folder / f"{stem}.kumapie.json").exists():
                continue
            folder.mkdir(parents=True, exist_ok=True)
            if not (folder / f"{stem}.mp4").exists():
                job["step"] = f"{i}/{len(urls)} {stem}: downloading"
                h = job["height"]
                _run(_yt() + ["--no-playlist", "-f", f"bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/bv*[ext=mp4][height<={h}]+ba[ext=m4a]/b[ext=mp4]",
                              "--merge-output-format", "mp4", "--write-subs", "--sub-langs", f"{lang}.*,{translation}.*",
                              "--convert-subs", "srt", "-o", f"{stem}.%(ext)s", url], cwd=folder)
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
                if lang == "ja":
                    _run([VENV / "python.exe", cfg.pipeline / "parakeet.py", "--ja", cfg.root / CONF["parakeet_ja"],
                          "-o", stem, f"{stem}.mp4"], cwd=folder)
                else:
                    _run([VENV / "python.exe", cfg.pipeline / "parakeet.py", "-o", stem, f"{stem}.mp4"], cwd=folder)
                    _run([cfg.python, cfg.pipeline / "srt_spaced.py", "-o", stem, f"{stem}.json"], cwd=folder)
            made.append(folder / f"{stem}.mp4")
            job["episodes"].append(stem)
        except Exception as e:  # noqa: BLE001 - one bad video doesn't stop the channel's others
            failed.append(f"{url}: {str(e)[:100]}")
    if made:
        job["step"] = "building episodes"
        _run([cfg.python, cfg.code / "tools" / "kumapie" / "pc_episodes.py", "--for", job["key"], *made])
    if failed:
        raise RuntimeError(f"{len(failed)} of {len(urls)} failed: " + "; ".join(failed))


def _process(job: dict) -> None:
    if job["kind"]:
        return _youtube(job)
    out =cfg.work / "aniworld" / job["id"]
    job["step"] = "downloading"
    # The CLI skips what it has: the second run retries only failed episodes (a host's hiccup). Its exit code isn't
    # checked: a failed single episode exits 1, a season with failed episodes exits 0; what's on disk is what counts.
    for _ in range(2):
        _run([VENV / "aniworld.exe", "-a", "Download", "-l", CONF["dub"][job["lang"]], "-o", out, job["url"]], check=False)
    videos = sorted(f for f in out.rglob("*") if f.suffix.lower() in (".mkv", ".mp4") and EPISODE.search(f.name)
                    and ".temp" not in f.name)  # a failed download leaves <name>.temp_full.mkv
    if not videos:
        shutil.rmtree(out, ignore_errors=True)
        raise RuntimeError(f"no {CONF['dub'][job['lang']]} for this link (or the hosts failed twice)")
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
            _run([VENV / "python.exe", cfg.pipeline / "parakeet.py", "-o", stem, f"{stem}.mp4"], cwd=folder)
            _run([cfg.python, cfg.pipeline / "srt_spaced.py", "-o", stem, f"{stem}.json"], cwd=folder)
        made.append(folder / f"{stem}.mp4")
        job["episodes"].append(stem)
        src.unlink()
    job["step"] = "building episodes"
    # Only this job's videos: the whole library would also list episodes that live only on a device (duplicates).
    _run([cfg.python, cfg.code / "tools" / "kumapie" / "pc_episodes.py", "--for", job["key"], *made])
    shutil.rmtree(out, ignore_errors=True)


def _worker() -> None:
    while True:
        job = _queue.get()
        job["state"] = "running"
        try:
            _process(job)
            job["state"], job["step"] = "done", ""
        except Exception as e:  # noqa: BLE001 - reported to the device
            job["state"], job["error"] = "failed", str(e)[:300]


def listing() -> list[dict]:
    return [{k: v for k, v in j.items() if k != "key"} for j in jobs]


threading.Thread(target=_worker, daemon=True).start()


if __name__ == "__main__":
    assert URL.match("https://aniworld.to/anime/stream/you-and-i-are-polar-opposites/staffel-2/episode-1")
    assert URL.match("https://s.to/serie/stream/the-office/staffel-1")
    assert not URL.match("https://aniworld.to/anime/stream/x/staffel-1; rm -rf /")
    assert not URL.match("https://evil.example/anime/stream/x")
    assert _slug("You and I Are Polar Opposites (2026-2026) [imdbid-tt37532731]") == "you_and_i_are_polar_opposites"
    assert YOUTUBE.match("https://www.youtube.com/shorts/Cn9o7nr_HFo") and YOUTUBE.match("https://youtu.be/abc?si=x")
    assert not YOUTUBE.match("https://www.youtube.com/watch?v=x; rm -rf /")
    assert CHANNEL.match("https://www.youtube.com/@marichannel/shorts").group(0) == "https://www.youtube.com/@marichannel"
    assert _ascii("Café Trip! Vlog #3") == "cafe_trip_vlog_3" and _ascii("コアラの指紋") == ""
    print("ok")
