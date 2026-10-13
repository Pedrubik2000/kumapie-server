"""AniWorld / s.to episodes for kumapie, made on the PC (config.toml [aniworld]).

  POST /api/aniworld   {person, lang, url}: person = a [sync.people] key (the device's owner), lang = the dub's language
                       (de/en), url = an aniworld.to or s.to show, season or episode link. Queued, one job at a time.
  GET  /api/aniworld   the jobs since the server started, newest first: [{id, person, lang, url, state, step, error,
                       episodes}]

A job: the aniworld CLI downloads the dub -> remux to library\\<key>\\anime\\<show>\\<show>_sN_EE.mp4 -> Parakeet
(pipeline\\parakeet.py) -> srt_spaced.py -> pc_episodes.py. tv.py's rescan lists it within a minute.
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid

from dojo import cfg
from dojo.config import safe_name

CONF = cfg.raw["aniworld"]
VENV = cfg.root / CONF["venv"] / "Scripts"
PEOPLE: dict[str, dict[str, str]] = cfg.raw.get("sync", {}).get("people", {})
URL = re.compile(r"^https://(aniworld\.to/anime|s\.to/serie|serienstream\.to/serie)/stream/[\w-]+"
                 r"(/staffel-\d+(/episode-\d+)?|/filme(/film-\d+)?)?/?$")
EPISODE = re.compile(r"S(\d+)E(\d+)", re.I)
LOG = cfg.work / "logs" / "aniworld.log"
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
ENV = {**os.environ, "PYTHONUTF8": "1", "HF_HOME": str(cfg.root / CONF["hf_home"]),
       "PATH": os.path.dirname(FFMPEG) + os.pathsep + os.environ.get("PATH", "")}  # aniworld and parakeet.py run ffmpeg

jobs: list[dict] = []
_queue: queue.Queue = queue.Queue()


def submit(body: dict) -> dict:
    person, lang, url = str(body.get("person", "")), str(body.get("lang", "")), str(body.get("url", "")).strip()
    if not URL.match(url):
        raise ValueError("not an aniworld.to / s.to link")
    key = PEOPLE.get(person, {}).get(lang)
    if not key or lang not in CONF["dub"]:
        raise ValueError(f"no {lang!r} library for {person!r}")
    job = {"id": uuid.uuid4().hex[:8], "person": person, "lang": lang, "url": url, "key": key,
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


def _slug(folder: str) -> str:
    """'You and I Are Polar Opposites (2026-2026) [imdbid-tt37532731]' -> 'you_and_i_are_polar_opposites'."""
    name = re.sub(r"\s*[(\[].*$", "", folder)
    return re.sub(r"[^\w]+", "_", safe_name(name).lower()).strip("_")


def _process(job: dict) -> None:
    out = cfg.work / "aniworld" / job["id"]
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
    print("ok")
