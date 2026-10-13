"""kumapie episodes made on a device, kept on the PC and handed to the person's other devices (the PC is the hub).

Plan: code\\kumapie_languages_plan.md, "PC sync and Android <-> Android". Served by server.py, tailnet only:

  GET  /api/sync/<person>                         the person's uploaded episodes, newest first: [{id, show, title,
                                                  lang, kind, season, number, duration, scenes, size, uploaded}]
  GET  /api/sync/<person>/<id>/<file>             episode.json | video.mp4 (HTTP Range) | thumb.jpg
  GET  /api/sync/<person>/<id>/status             {file: bytes received so far} (an upload in progress)
  PUT  /api/sync/<person>/<id>/<file>?offset=N    the next bytes of that file; N must be what was received so far
                                                  (409 + {"size": n} when not), so an upload resumes after a drop
  POST /api/sync/<person>/<id>/episode            {"shifted": s, "episode": {...}}: the device moved the subtitles
                                                  (player > Subtitle timing): replaces the library's .kumapie.json and
                                                  .srt files; the listing's "shifted" tells the other devices to refetch
  POST /api/sync/<person>/<id>/delete           "Delete everywhere" on a device: the library files go to
                                                  archive/removed/<date>/, the listing drops it, and the id is
                                                  remembered so the person's other devices drop their copies
  GET  /api/sync/<person>/deleted                 those ids
  POST /api/sync/<person>/<id>/done               {show, title, lang, translation, kind, season, number, duration, source}:
                                                  moves the files into library\\<key>\\<kind>\\<show>\\ with the
                                                  library's names (<stem>.mp4, .srt, .<translation>.srt,
                                                  .kumapie.json, .jpg) and lists it

<person> is a key of [sync.people] in config.toml (pedro, alice, bob); the episode's language picks the library.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from glob import escape as glob_escape
from pathlib import Path

from dojo import cfg
from dojo.config import safe_name

PEOPLE: dict[str, dict[str, str]] = cfg.raw.get("sync", {}).get("people", {})
STATE = cfg.state / "feed"
INDEX = STATE / "sync.json"  # {person: {id: entry + "dir" (relative to the library root) + "stem"}}
UPLOADS = STATE / "sync-upload"
DELETED = STATE / "sync-deleted.json"  # {person: [id, ...]}: deleted everywhere
FILES = {"episode.json", "video.mp4", "thumb.jpg"}
LIBRARY_EXT = {"episode.json": ".kumapie.json", "video.mp4": ".mp4", "thumb.jpg": ".jpg"}
ID = re.compile(r"^local-[0-9a-f]{12}$")
KIND_DIR = {"anime": "anime", "shows": "shows", "movies": "movies", "youtube": "youtube", "reels": "youtube"}
PRIVATE = ("dir", "stem")

_lock = threading.Lock()


def _index() -> dict:
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(index: dict) -> None:
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(INDEX)


def _public(e: dict) -> dict:
    return {k: v for k, v in e.items() if k not in PRIVATE}


def valid(person: str, eid: str, name: str | None = None) -> bool:
    return person in PEOPLE and bool(ID.match(eid)) and (name is None or name in FILES)


def listing(person: str) -> list[dict]:
    return sorted((_public(e) for e in _index().get(person, {}).values()), key=lambda e: e.get("uploaded", ""), reverse=True)


def stored(person: str, eid: str, name: str) -> Path | None:
    """The library file behind an uploaded episode's episode.json / video.mp4 / thumb.jpg."""
    e = _index().get(person, {}).get(eid)
    if not e:
        return None
    f = cfg.library_root / e["dir"] / (e["stem"] + LIBRARY_EXT[name])
    return f if f.is_file() else None


def deleted(person: str) -> list[str]:
    try:
        return json.loads(DELETED.read_text(encoding="utf-8")).get(person, [])
    except (OSError, ValueError):
        return []


def delete(person: str, eid: str) -> dict:
    """Deletes an uploaded episode everywhere: its library files to archive/removed/<date>/ (not gone for good)."""
    with _lock:
        index = _index()
        e = index.get(person, {}).pop(eid, None)
        if e:
            src = cfg.library_root / e["dir"]
            out = cfg.archive / "removed" / time.strftime("%Y-%m-%d") / e["dir"]
            out.mkdir(parents=True, exist_ok=True)
            for f in src.glob(glob_escape(e["stem"]) + ".*"):
                if f.name.split(".")[0] == e["stem"]:  # x.mp4, x.en.srt, x.kumapie.json; not x_2.mp4
                    shutil.move(str(f), out / f.name)
            if src.is_dir() and not any(src.iterdir()):
                src.rmdir()
            _save(index)
        remember_deleted(person, eid)
    return {"id": eid, "deleted": bool(e), "show": (e or {}).get("show", ""), "title": (e or {}).get("title", "")}


def remember_deleted(person: str, eid: str) -> None:
    """[eid] in /api/sync/<person>/deleted: the person's other devices drop it (an episode, or a book: book_sync.py)."""
    try:
        gone = json.loads(DELETED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        gone = {}
    if eid not in gone.setdefault(person, []):
        gone[person].append(eid)
        DELETED.write_text(json.dumps(gone, indent=1), encoding="utf-8")


def status(person: str, eid: str) -> dict:
    d = UPLOADS / person / eid
    return {n: (d / n).stat().st_size for n in FILES if (d / n).is_file()}


def receive(person: str, eid: str, name: str, offset: int, read, length: int) -> int:
    """Appends [length] bytes from [read] at [offset]; answers the new size. ValueError(size) when offset is wrong."""
    d = UPLOADS / person / eid
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    have = f.stat().st_size if f.exists() else 0
    if offset != have:
        raise ValueError(have)
    with f.open("ab") as out:
        left = length
        while left > 0:
            chunk = read(min(left, 1 << 20))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)
    return f.stat().st_size


def _srt_time(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def srt(lines: list[tuple[float, float, str]]) -> str:
    return "".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(lines, 1))


def _slug(s: str) -> str:
    return re.sub(r"[^\w]+", "_", s.lower()).strip("_") or "episode"


def _write(out: Path, stem: str, episode: dict, translation: str) -> None:
    """The episode's .kumapie.json and, for the PC's tools, its .srt (spoken lines) and .<translation>.srt."""
    spoken, translated = [], []
    for sc in episode.get("scenes", []):
        spoken += [(c["start"], c["end"], "".join(s[0] for s in c.get("seg", []))) for c in sc.get("cues", [])]
        translated += [(a, b, t) for a, b, t in sc.get("english", [])]
    (out / f"{stem}.srt").write_text(srt(spoken), encoding="utf-8")
    if translated:
        (out / f"{stem}.{translation}.srt").write_text(srt(translated), encoding="utf-8")
    episode.pop("video", None)  # a path on the device that made it
    (out / f"{stem}.kumapie.json").write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")


def retime(person: str, eid: str, episode: dict, shifted: float) -> dict:
    """A listed episode whose subtitles were moved on a device: its new files, and "shifted" (the device's total) in
    the listing, so the other devices fetch episode.json again. ValueError when it isn't listed."""
    with _lock:
        index = _index()
        e = index.get(person, {}).get(eid)
        if not e or not isinstance(episode.get("scenes"), list):
            raise ValueError("not an uploaded episode")
        out = cfg.library_root / e["dir"]
        # The translation .srt already there keeps its language (stem.en.srt / stem.es.srt).
        tr = next((f.name[len(e["stem"]) + 1:-4] for f in out.glob(f"{e['stem']}.*.srt")), "en")
        _write(out, e["stem"], episode, tr)
        e["shifted"] = round(float(shifted), 2)
        _save(index)
    return _public(e)


def finish(person: str, eid: str, meta: dict) -> dict:
    """Moves a complete upload into the library and lists it. ValueError when it can't."""
    d = UPLOADS / person / eid
    if not (d / "episode.json").is_file() or not (d / "video.mp4").is_file():
        raise ValueError("episode.json and video.mp4 are needed")
    episode = json.loads((d / "episode.json").read_text(encoding="utf-8"))
    lang = str(meta.get("lang") or episode.get("lang") or "de")
    key = PEOPLE[person].get(lang)
    if not key:
        raise ValueError(f"no library for {person} in {lang}")
    kind = str(meta.get("kind") or "youtube")
    show = str(meta.get("show") or episode.get("show") or "kumapie")
    title = str(meta.get("title") or episode.get("title") or eid)
    season, number = meta.get("season"), meta.get("number")
    folder = Path(key) / KIND_DIR.get(kind, "youtube") / safe_name(_slug(show))
    base = safe_name(f"{_slug(show)}_s{season or 1}_{int(number):02}" if number is not None else _slug(title)[:80])
    out = cfg.library_root / folder
    out.mkdir(parents=True, exist_ok=True)
    with _lock:
        index = _index()
        mine = index.get(person, {}).get(eid)
        # Same name as another file in the folder (two videos with one title): _2, _3...
        stem, n = (mine["stem"] if mine and mine["dir"] == str(folder) else base), 2
        while not mine and (out / f"{stem}.mp4").exists():
            stem, n = f"{base}_{n}", n + 1
        # The device says which language its translation line is in (Spanish when made with "I speak: Español").
        _write(out, stem, episode, str(meta.get("translation") or (cfg.person(key).translation if key in cfg.people else "en")))
        shutil.move(str(d / "video.mp4"), out / f"{stem}.mp4")
        if (d / "thumb.jpg").is_file():
            shutil.move(str(d / "thumb.jpg"), out / f"{stem}.jpg")
        shutil.rmtree(d, ignore_errors=True)
        entry = {"id": eid, "show": show, "title": title, "lang": lang, "kind": kind, "season": season, "number": number,
                 "duration": meta.get("duration") or episode.get("duration"), "scenes": len(episode.get("scenes", [])),
                 "source": meta.get("source", ""), "size": (out / f"{stem}.mp4").stat().st_size,
                 "uploaded": time.strftime("%Y-%m-%dT%H:%M:%S"), "dir": str(folder), "stem": stem,
                 "match": meta.get("match")}  # the show's metadata picked on the phone ("source:id"), item 23
        index.setdefault(person, {})[eid] = entry
        _save(index)
    return _public(entry)


if __name__ == "__main__":  # self-check: srt timing
    assert srt([(0.882, 4.242, "Hi")]) == "1\n00:00:00,882 --> 00:00:04,242\nHi\n\n"
    assert _srt_time(3723.5) == "01:02:03,500"
    assert valid("pedro", "local-07271f271935", "video.mp4") and not valid("pedro", "../x", None)
    # retime: rewrites the listed episode's files in place, keeps its .en.srt language, lists "shifted".
    import tempfile, types
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); (root / "ja/anime/x").mkdir(parents=True); (root / "ja/anime/x/x_s1_01.en.srt").write_text("old")
        idx = {"pedro": {"local-000000000001": {"id": "local-000000000001", "show": "X", "title": "1", "dir": "ja/anime/x", "stem": "x_s1_01"}}}
        cfg, _index, _save = types.SimpleNamespace(library_root=root), (lambda: idx), (lambda i: None)
        ep = {"scenes": [{"cues": [{"start": 1.5, "end": 2.0, "seg": [["あ", None]]}], "english": [[1.5, 2.0, "Ah"]]}], "video": "/sdcard/v.mp4"}
        assert retime("pedro", "local-000000000001", ep, 0.5)["shifted"] == 0.5
        assert (root / "ja/anime/x/x_s1_01.en.srt").read_text(encoding="utf-8") == "1\n00:00:01,500 --> 00:00:02,000\nAh\n\n"
        assert "video" not in json.loads((root / "ja/anime/x/x_s1_01.kumapie.json").read_text(encoding="utf-8"))
        try:
            retime("pedro", "local-000000000002", ep, 1); raise AssertionError("unlisted episode accepted")
        except ValueError:
            pass
        # delete: the episode's own files to the archive (not its neighbour x_s1_01_2), listed as deleted.
        (root / "ja/anime/x/x_s1_01.mp4").write_text("v"); (root / "ja/anime/x/x_s1_01_2.mp4").write_text("v2")
        cfg.archive, DELETED = root / "archive", root / "deleted.json"
        assert delete("pedro", "local-000000000001")["deleted"] and deleted("pedro") == ["local-000000000001"]
        assert (root / "ja/anime/x/x_s1_01_2.mp4").exists() and not (root / "ja/anime/x/x_s1_01.mp4").exists()
        moved = {f.name for f in (root / "archive/removed").rglob("*.*")}
        assert "x_s1_01.mp4" in moved and "x_s1_01.en.srt" in moved and "x_s1_01_2.mp4" not in moved
    print("ok")
