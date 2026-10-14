"""Library episodes -> kumapie episodes on the PC: <stem>.kumapie.json next to each video, built from its subtitles
(<stem>.srt = the spoken language, <stem>.<translation>.srt = the line under it) with kumapie's own newepisode.build,
so phones list, stream and download them from dojo-feed (services\\feed\\tv.py, `?person=`).

Run with the repo's venv (spaCy + both models); name the new videos, or it walks the whole library:
  .venv\\Scripts\\python.exe tools\\kumapie\\pc_episodes.py --for alice [--force] [<new .mp4 files>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(CODE), str(CODE / "tools" / "feed")]
from dojo import cfg  # noqa: E402
sys.path.insert(0, str(cfg.kumapie_app / "mobile" / "src" / "main" / "python"))
from feed import read_srt  # noqa: E402
import newepisode  # noqa: E402
import ja_words  # noqa: E402
from dojo.config import Person  # noqa: E402

MODELS = {"de": "de_core_news_lg", "en": "en_core_web_md"}  # the ones the phones download
EPISODE = re.compile(r"_s(\d+)_(\d+)$")


def duration(video: Path) -> float:
    out = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout
    return float(out.strip() or 0)


def show_title(folder: str) -> str:
    """tastefully_yours_2025 -> Tastefully Yours (the library folder is the TMDB title + year)."""
    return re.sub(r"_\d{4}$", "", folder).replace("_", " ").title()


def synced(key: str) -> Person:
    """A library only in [sync.people] (pedro-ja, pedro-en): its language from there, English under it."""
    lang = next(l for langs in cfg.raw["sync"]["people"].values() for l, k in langs.items() if k == key)
    return Person(key=key, name=key, transcript=lang, translation="en")


def make(video: Path, person, force: bool) -> bool:
    out = video.with_name(video.stem + ".kumapie.json")
    spoken = video.with_suffix(".srt")
    if (out.exists() and not force) or not spoken.exists():
        return False
    translated = video.with_name(f"{video.stem}.{person.translation}.srt")
    rel = video.relative_to(cfg.library_root).as_posix()
    eid = hashlib.sha1(rel.encode()).hexdigest()[:12]  # as tv.episode_id: the path is the source
    kind = video.relative_to(cfg.library(person.key)).parts[0]
    m = EPISODE.search(video.stem)
    season, number = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    show = show_title(video.parent.name)
    named = video.with_suffix(".title.txt")  # YouTube's own title (services\feed\aniworld.py)
    title = (named.read_text(encoding="utf-8").strip() if named.exists() else
             f"Episode {number}" if number is not None else video.stem.replace("_", " ").title())
    cues = [[c.start, c.end, c.text] for c in read_srt(spoken)]
    english = [[c.start, c.end, c.text] for c in read_srt(translated)] if translated.exists() else []
    # Japanese: the phones' own splitter (ja_words), as tools\vn\higurashi.py; German / English: spaCy.
    ja = person.transcript == "ja"
    episode = json.loads(newepisode.build("" if ja else MODELS[person.transcript], eid, show, title, duration(video), "",
                                          json.dumps(cues), json.dumps(english), person.transcript,
                                          json.dumps(ja_words.parsed([c[2] for c in cues])) if ja else ""))
    episode.pop("video")
    episode.update(kind=kind, season=season, number=number, source=rel)
    out.write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--for", dest="person", required=True, help="library key (alice, bob, ...)")
    ap.add_argument("--force", action="store_true", help="rebuild episodes that have one already")
    ap.add_argument("videos", nargs="*", type=Path,
                    help="only these .mp4 files (default: the whole library, which also lists videos whose episode "
                         "lives only on a device - duplicates)")
    a = ap.parse_args()
    person = cfg.people.get(a.person) or synced(a.person)
    for video in sorted(a.videos or cfg.library(a.person).rglob("*.mp4")):
        if video.name.endswith(".condensed.mp4"):
            continue
        if make(video, person, a.force):
            print("made", video.relative_to(cfg.library_root), flush=True)


if __name__ == "__main__":
    main()
