"""NuvioTV (or any Stremio app) -> a kumapie episode made on the PC (kumapie_plan.md item 5a).

A Stremio add-on per person: every movie or episode gets one stream per language of that person's libraries (anime,
Kitsu ids: Japanese only), plus "whole season" for an episode, whose only link is kumapie://prepare?... NuvioTV opens
such a link straight in kumapie (VIEW + BROWSABLE, no "Open with" menu, which hides kumapie on the TCL TV). kumapie
then asks the PC to prepare it: Torrentio finds a Real-Debrid file (a season: a season pack), jobs.py makes the library
episodes (their own subtitles, else Jimaku, else Parakeet), and kumapie plays the picked one from the PC like any other
as soon as it's made. Opening an episode also queues the next two (they're ready when it ends).

  GET  /addon/<person>/manifest.json            the add-on (install: stremio://<host>/addon/<person>/manifest.json)
  GET  /addon/<person>/stream/<type>/<id>.json  {"streams": [{"name", "title", "externalUrl": "kumapie://prepare?..."}]}
  POST /api/tv/prepare  {"person", "lang", "type", "id", "season"?: "1"} -> {"state": "ready", "id": episode} or
                        {"state": "queued" | "running" | "failed", "step", "error"}: ask again until ready
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dojo import cfg
import jobs
import sync

TORRENTIO = "https://torrentio.strem.fun"
DONE = cfg.state / "feed" / "stremio.json"  # {"<person>|<lang>|<id>": episode id}: asking again is instant
NAMES = {"de": "Deutsch", "en": "English", "ja": "日本語"}
ID = re.compile(r"^(tt\d+|kitsu:\d+)(:\d+:\d+|:\d+)?$")
SIZE = re.compile(r"💾\s*([\d.]+)\s*([KMGT]B)")
QUALITIES = ["BD", "4k", "2k", "1080p", "720p", "480p"]  # the add-on's order
RESOLUTION = {"4k": r"\b(4k|2160p)\b", "2k": r"\b(2k|1440p)\b", "1080p": r"\b1080p\b", "720p": r"\b720p\b",
              "480p": r"\b(480p|576p|sd)\b"}
HASH = re.compile(r"/realdebrid/[^/]+/([0-9a-fA-F]{40})/")
AHEAD = 2  # episodes queued after the one being watched
_lock = threading.Lock()
_running: dict[str, dict] = {}  # key -> its jobs.py job (or a stand-in while Torrentio is asked)


def manifest(person: str) -> dict:
    return {"id": f"dojo.kumapie.{person}", "version": "1.1.0", "name": "kumapie",
            "description": "Watch it in kumapie: the PC makes the episode first (subtitles, words).",
            "resources": ["stream"], "types": ["movie", "series"], "idPrefixes": ["tt", "kitsu"], "catalogs": []}


def streams(person: str, type_: str, sid: str) -> dict:
    """One entry per quality Torrentio has (with the file's size), language and episode / whole season."""
    langs = [lang for lang in (["ja"] if sid.startswith("kitsu:") else sync.PEOPLE[person]) if lang in sync.PEOPLE[person]]
    seasons = [False, True] if numbers(sid)[1] is not None else [False]
    try:
        found = _found(type_, sid, os.environ.get("RD_API_TOKEN", ""))
    except OSError:  # Torrentio down: no entries rather than an error in NuvioTV
        found = []
    out = []
    for q in QUALITIES:
        if not any(q in qualities(s) for s in found):
            continue
        for season in seasons:
            best = pick(found, season, q)
            if season and not _pack(best):
                continue  # no season pack in this quality
            gb = _size(best["title"])
            size = (f"{gb:.1f} GB" if gb >= 1 else f"{gb * 1000:.0f} MB") + (" an episode" if season else "")
            if q == "BD":  # which resolution the Blu-ray file is
                size = " ".join(sorted(qualities(best) - {"BD"})) + " · " + size
            slow = "" if "RD+" in best.get("name", "") else " · not on Real-Debrid yet (slower)"
            for lang in langs:
                out.append({"name": f"kumapie\n{q}",
                            "title": f"{NAMES.get(lang, lang)} · {'whole season' if season else 'this one'} · 💾 {size}{slow}",
                            "externalUrl": "kumapie://prepare?" + urllib.parse.urlencode(
                                {"person": person, "lang": lang, "type": type_, "id": sid, "q": q,
                                 **({"season": "1"} if season else {})})})
    return {"streams": out}


def qualities(s: dict) -> set[str]:
    """A Torrentio stream's qualities: its resolution (the name's second line), plus BD for a Blu-ray source."""
    name, title = s.get("name", "").lower(), s.get("title", "").lower()
    got = {q for q, pat in RESOLUTION.items() if re.search(pat, name)}
    if re.search(r"blu-?ray|\bbd(rip|remux)?\b|remux", title):
        got.add("BD")
    return got


def numbers(sid: str) -> tuple[int | None, int | None]:
    """(season, episode) of a Stremio id: tt…:1:2 -> (1, 2), kitsu:…:5 -> (None, 5), a movie -> (None, None)."""
    nums = [int(n) for n in sid.split(":")[1 if sid.startswith("tt") else 2:]]
    return (nums[0], nums[1]) if len(nums) == 2 else (None, nums[0]) if nums else (None, None)


def _get(url: str, **kw):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "kumapie"}), timeout=30, **kw)


def _size(title: str) -> float:
    m = SIZE.search(title)
    return float(m.group(1)) * {"KB": 1e-6, "MB": 1e-3, "GB": 1, "TB": 1e3}[m.group(2)] if m else 1e9


def _pack(s: dict) -> bool:
    """Torrentio's title starts with the torrent's name: a pack's isn't the file's."""
    name = Path((s.get("behaviorHints") or {}).get("filename") or "").stem
    return bool(name) and name not in s.get("title", "").split("\n")[0]


def pick(found: list[dict], pack: bool = False, q: str = "1080p") -> dict:
    """Torrentio's streams -> the one to download: quality [q] if there is one, cached on Real-Debrid, smallest file
    (the PC downloads it all); [pack]: a season pack if there is one."""
    found = [s for s in found if q in qualities(s)] or found
    found = [s for s in found if _pack(s)] or found if pack else found
    cached = [s for s in found if "RD+" in s.get("name", "")] or found
    return min(cached, key=lambda s: _size(s.get("title", "")))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _found(type_: str, sid: str, token: str) -> list[dict]:
    with _get(f"{TORRENTIO}/realdebrid={token}/stream/{type_}/{urllib.parse.quote(sid)}.json") as r:
        return json.load(r).get("streams", [])


def _link(type_: str, sid: str, token: str, pack: bool, q: str) -> str:
    """The Real-Debrid link of the best Torrentio file for this movie or episode; [pack]: its season pack's magnet."""
    found = _found(type_, sid, token)
    if not found:
        raise ValueError("Torrentio found nothing for this")
    best = pick(found, pack, q)
    if pack and (m := HASH.search(best["url"])):
        return f"magnet:?xt=urn:btih:{m[1]}"
    # Torrentio's resolve link answers 302 to the file on Real-Debrid's servers.
    try:
        urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(best["url"], headers={"User-Agent": "kumapie"}), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 307) and e.headers.get("Location"):
            return e.headers["Location"]
        raise
    raise ValueError("Real-Debrid gave no file")


def _done() -> dict:
    try:
        return json.loads(DONE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _made(tv, lib: str, job: dict, number: int | None) -> str | None:
    """The episode [job] made for episode [number] (a movie: any), once it's listed."""
    stems = [s for s in job["episodes"] if number is None or s.endswith(f"_{number:02}")]
    if not stems:
        return None
    tv.synced(force=True)
    return next((e["id"] for e in tv.episodes.values()
                 if e["person"] == lib and isinstance(e.get("path"), Path) and e["path"].stem in stems), None)


def _folder(tv, base: str, sid: str) -> str:
    """The library folder of this show's episodes made from NuvioTV before (any season), or ""."""
    show = base + ":".join(sid.split(":")[:2 if sid.startswith("kitsu:") else 1]) + ":"
    for k, eid in _done().items():
        path = tv.episodes.get(eid, {}).get("path")
        if k.startswith(show) and isinstance(path, Path):
            return str(path.parent)
    return ""


def _start(key: str, person: str, lang: str, type_: str, sid: str, season: bool, q: str, folder: str = "") -> dict:
    """Queues the job in the background (Torrentio and Real-Debrid take seconds); answers its stand-in."""
    s, e = numbers(sid)
    job = _running[key] = {"state": "queued", "step": "asking Torrentio", "error": "", "episodes": []}

    def run():
        token = os.environ.get("RD_API_TOKEN", "")
        try:
            got = jobs.submit({"person": person, "lang": lang, "url": _link(type_, sid, token, season, q), "rd": token,
                               "jimaku": os.environ.get("JIMAKU_API_KEY", "") if lang == "ja" else "",
                               "type": "anime" if sid.startswith("kitsu:") else "movies" if type_ == "movie" else "shows",
                               **({"season": s or 0, "first": e, "folder": folder} if season else {})})
            live = next(j for j in jobs.jobs if j["id"] == got["id"])  # the live one, its state changes
        except (ValueError, OSError) as err:
            job.update(state="failed", error=str(err)[:200])
            return
        with _lock:
            if _running.get(key) is job:
                _running[key] = live
    threading.Thread(target=run, daemon=True).start()
    return job


def _ready(tv, lib: str, key: str, skey: str | None, number: int | None) -> str | None:
    eid = _done().get(key)
    if eid in tv.episodes:
        return eid
    for k in (key, skey):
        job = _running.get(k) if k else None
        if job and (eid := _made(tv, lib, job, number)):
            DONE.write_text(json.dumps({**_done(), key: eid}), encoding="utf-8")
            return eid
    return None


def prepare(tv, body: dict) -> dict:
    person, lang = str(body.get("person", "")), str(body.get("lang", ""))
    type_, sid, season = str(body.get("type", "")), str(body.get("id", "")), body.get("season") == "1"
    q = str(body.get("q") or "1080p")
    if q not in QUALITIES:
        raise ValueError(f"unknown quality {q!r}")
    lib = sync.PEOPLE.get(person, {}).get(lang)
    if not lib or type_ not in ("movie", "series") or not ID.match(sid):
        raise ValueError("bad prepare request")
    number = numbers(sid)[1]
    base = f"{person}|{lang}|"
    key = base + sid
    skey = base + sid.rsplit(":", 1)[0] + "|season" if number is not None else None
    with _lock:
        tv.synced()
        if season and skey and skey not in _running:  # even when this episode is made: the rest of the season
            _start(skey, person, lang, type_, sid, True, q, _folder(tv, base, sid))
        if eid := _ready(tv, lib, key, skey, number):
            answer = {"state": "ready", "id": eid}
        else:
            job = _running.get(key) or (_running.get(skey) if skey else None)
            if job and job["state"] in ("failed", "done"):
                # Reported once, then picking it again starts over. A done season job stays (no second pack); one
                # without this episode: the episode alone.
                if _running.get(key) is job or job["state"] == "failed":
                    for k in (key, skey):
                        if k and _running.get(k) is job:
                            del _running[k]
                if job["state"] == "failed":
                    return {"state": "failed", "step": "", "error": job["error"]}
                job = None
            if not job:
                job = _start(key, person, lang, type_, sid, False, q)
            answer = {"state": job["state"], "step": job["step"], "error": job["error"]}
        if number is not None and not season:
            for n in range(number + 1, number + 1 + AHEAD):  # ready by the time this one ends
                nid = sid.rsplit(":", 1)[0] + f":{n}"
                if base + nid not in _running and skey not in _running and _done().get(base + nid) not in tv.episodes:
                    _start(base + nid, person, lang, type_, nid, False, q)
        return answer


if __name__ == "__main__":
    t = [{"name": "[RD+] Torrentio\n4k", "title": "x\n💾 12.1 GB", "url": "a"},
         {"name": "[RD+] Torrentio\n1080p", "title": "y\n💾 2.3 GB", "url": "b"},
         {"name": "[RD+] Torrentio\n1080p", "title": "z\n💾 900 MB", "url": "c"},
         {"name": "[RD download] Torrentio\n1080p", "title": "w\n💾 100 MB", "url": "d"},
         {"name": "[RD+] Torrentio\n1080p", "title": "Show S01 1080p\nShow S01E01.mkv\n💾 1.1 GB", "url": "e",
          "behaviorHints": {"filename": "Show S01E01.mkv"}}]
    assert pick(t)["url"] == "c" and pick(t, pack=True)["url"] == "e", pick(t)
    assert pick(t[:4], pack=True)["url"] == "c"  # no pack: the best file
    assert qualities(t[0]) == {"4k"} and qualities({"name": "x\n1080p", "title": "Show BluRay 1080p"}) == {"1080p", "BD"}
    assert pick(t, q="4k")["url"] == "a" and pick(t, q="720p")["url"] == "c"  # none in 720p: the best of all
    assert ID.match("kitsu:43248:1") and ID.match("tt0903747:1:2") and ID.match("tt0111161")
    assert not ID.match("tt1/../x")
    assert numbers("tt0903747:1:2") == (1, 2) and numbers("kitsu:43248:5") == (None, 5) and numbers("tt1") == (None, None)
    assert HASH.search("https://torrentio.strem.fun/resolve/realdebrid/KEY/" + "a" * 40 + "/null/0/x.mkv")[1] == "a" * 40
    print("ok")
