"""NuvioTV (or any Stremio app) -> a kumapie episode made on the PC (kumapie_plan.md item 5a).

A Stremio add-on per person: every movie or episode gets one stream per quality Torrentio has (with the file's size),
language of that person's libraries (anime, Kitsu ids: Japanese only) and "this one" / "whole season", whose only link
is kumapie://prepare?... NuvioTV opens such a link straight in kumapie (VIEW + BROWSABLE, no "Open with" menu, which
hides kumapie on the TCL TV). kumapie then asks the PC to prepare it: Torrentio finds the Real-Debrid file, jobs.py
makes the library episode (its own subtitles, else Jimaku, else Parakeet), and kumapie plays it from the PC like any
other. Episodes are named from Stremio's catalog (Cinemeta, Kitsu), not from file names: show, season, number. A
season queues each of its episodes (the picked one first, from the same torrent when it has them); opening an episode
queues the next two (into the next season too).

  GET  /addon/<person>/manifest.json            the add-on (install: stremio://<host>/addon/<person>/manifest.json)
  GET  /addon/<person>/stream/<type>/<id>.json  {"streams": [{"name", "title", "externalUrl": "kumapie://prepare?..."}]}
  POST /api/tv/prepare  {"person", "lang", "type", "id", "q"?, "season"?: "1"} -> {"state": "ready", "id": episode} or
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
META = {"tt": "https://v3-cinemeta.strem.io", "kitsu": "https://anime-kitsu.strem.fun"}
DONE = cfg.state / "feed" / "stremio.json"  # {"<person>|<lang>|<id>": episode id}: asking again is instant
NAMES = {"de": "Deutsch", "en": "English", "ja": "日本語"}
ID = re.compile(r"^(tt\d+|kitsu:\d+)(:\d+:\d+|:\d+)?$")
SIZE = re.compile(r"💾\s*([\d.]+)\s*([KMGT]B)")
HASH = re.compile(r"/realdebrid/[^/]+/([0-9a-fA-F]{40})/")
QUALITIES = ["BD", "4k", "2k", "1080p", "720p", "480p"]  # the add-on's order
RESOLUTION = {"4k": r"\b(4k|2160p)\b", "2k": r"\b(2k|1440p)\b", "1080p": r"\b1080p\b", "720p": r"\b720p\b",
              "480p": r"\b(480p|576p|sd)\b"}
AHEAD = 2  # episodes queued after the one being watched
_lock = threading.Lock()
_running: dict[str, dict] = {}  # key -> its jobs.py job (or a stand-in until it's queued)
_metas: dict[str, dict] = {}


def manifest(person: str) -> dict:
    return {"id": f"dojo.kumapie.{person}", "version": "1.2.0", "name": "kumapie",
            "description": "Watch it in kumapie: the PC makes the episode first (subtitles, words).",
            "resources": ["stream"], "types": ["movie", "series"], "idPrefixes": ["tt", "kitsu"], "catalogs": []}


def streams(person: str, type_: str, sid: str) -> dict:
    """One entry per quality Torrentio has (with the file's size), language and episode / whole season."""
    langs = [lang for lang in (["ja"] if sid.startswith("kitsu:") else sync.PEOPLE[person]) if lang in sync.PEOPLE[person]]
    seasons = [False, True] if type_ == "series" else [False]
    try:
        found = _found(type_, sid, os.environ.get("RD_API_TOKEN", ""))
    except OSError:  # Torrentio down: no entries rather than an error in NuvioTV
        found = []
    out = []
    for q in QUALITIES:
        if not any(q in qualities(s) for s in found):
            continue
        best = pick(found, q)
        gb = _size(best["title"])
        size = f"{gb:.1f} GB" if gb >= 1 else f"{gb * 1000:.0f} MB"
        if q == "BD":  # which resolution the Blu-ray file is
            size = " ".join(sorted(qualities(best) - {"BD"})) + " · " + size
        slow = "" if "RD+" in best.get("name", "") else " · not on Real-Debrid yet (slower)"
        for season in seasons:
            for lang in langs:
                out.append({"name": f"kumapie\n{q}",
                            "title": f"{NAMES.get(lang, lang)} · {'whole season' if season else 'this one'} · 💾 {size}"
                                     f"{' an episode' if season else ''}{slow}",
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


def _get(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "kumapie"}), timeout=30)


def _size(title: str) -> float:
    m = SIZE.search(title)
    return float(m.group(1)) * {"KB": 1e-6, "MB": 1e-3, "GB": 1, "TB": 1e3}[m.group(2)] if m else 1e9


def pick(found: list[dict], q: str = "1080p", torrent: str = "") -> dict:
    """Torrentio's streams -> the one to download: the file of [torrent] (a season's episodes from one release) if
    it's there, else quality [q] if there is one, cached on Real-Debrid, the smallest file (the PC downloads it all)."""
    same = [s for s in found if torrent and (m := HASH.search(s.get("url", ""))) and m[1].lower() == torrent]
    if same:
        return same[0]
    found = [s for s in found if q in qualities(s)] or found
    cached = [s for s in found if "RD+" in s.get("name", "")] or found
    return min(cached, key=lambda s: _size(s.get("title", "")))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _found(type_: str, sid: str, token: str) -> list[dict]:
    with _get(f"{TORRENTIO}/realdebrid={token}/stream/{type_}/{urllib.parse.quote(sid)}.json") as r:
        return json.load(r).get("streams", [])


def _link(type_: str, sid: str, token: str, q: str, torrent: str) -> tuple[str, str]:
    """(The Real-Debrid link of the file for this movie or episode, its torrent's hash)."""
    found = _found(type_, sid, token)
    if not found:
        raise ValueError("Torrentio found nothing for this")
    best = pick(found, q, torrent)
    m = HASH.search(best["url"])
    # Torrentio's resolve link answers 302 to the file on Real-Debrid's servers.
    try:
        urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(best["url"], headers={"User-Agent": "kumapie"}), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 307) and e.headers.get("Location"):
            return e.headers["Location"], m[1].lower() if m else ""
        raise
    raise ValueError("Real-Debrid gave no file")


def meta(type_: str, sid: str) -> dict:
    """The title in Stremio's catalog: {"name", "episodes": [[id, season, number]] in order} (specials left out).
    Kitsu has a title per season, numbered from 1, so its season is 1; a movie has no episodes."""
    base = ":".join(sid.split(":")[:2 if sid.startswith("kitsu:") else 1])
    if base not in _metas:
        with _get(f"{META['kitsu' if base.startswith('kitsu:') else 'tt']}/meta/{type_}/{base}.json") as r:
            m = json.load(r).get("meta") or {}
        eps = sorted(([v["id"], v.get("season") or 1 if base.startswith("tt") else 1, v.get("episode") or v.get("number")]
                      for v in m.get("videos") or [] if ID.match(v.get("id", "")) and v.get("season", 1) != 0),
                     key=lambda e: (e[1], e[2] or 0))
        _metas[base] = {"name": m.get("name") or base, "episodes": eps}
    return _metas[base]


def numbers(sid: str) -> tuple[int | None, int | None]:
    """(season, episode) of a Stremio id: tt…:1:2 -> (1, 2), kitsu:…:5 -> (1, 5), a movie -> (None, None)."""
    nums = [int(n) for n in sid.split(":")[1 if sid.startswith("tt") else 2:]]
    return (nums[0], nums[1]) if len(nums) == 2 else (1, nums[0]) if nums else (None, None)


def _done() -> dict:
    try:
        return json.loads(DONE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _made(tv, lib: str, job: dict) -> str | None:
    """The episode [job] made (one episode a job), once it's listed."""
    if not job["episodes"]:
        return None
    tv.synced(force=True)
    return next((e["id"] for e in tv.episodes.values() if e["person"] == lib and isinstance(e.get("path"), Path)
                 and e["path"].stem in job["episodes"]), None)


def _queue(base: str, person: str, lang: str, type_: str, sids: list[str], q: str) -> None:
    """Queues one job per id, in order, from one torrent where it has them (in the background: Torrentio and
    Real-Debrid take seconds); stand-ins answer until then."""
    sids = [s for s in sids if base + s not in _running]
    stand = {s: {"state": "queued", "step": "asking Torrentio", "error": "", "episodes": []} for s in sids}
    _running.update({base + s: j for s, j in stand.items()})

    def run():
        token, torrent = os.environ.get("RD_API_TOKEN", ""), ""
        for sid in sids:
            try:
                about = meta(type_, sid) if type_ == "series" else {"name": ""}
                season, number = numbers(sid)
                link, torrent = _link(type_, sid, token, q, torrent)
                got = jobs.submit({"person": person, "lang": lang, "url": link, "rd": token, "show": about["name"],
                                   "jimaku": os.environ.get("JIMAKU_API_KEY", "") if lang == "ja" else "",
                                   "type": "anime" if sid.startswith("kitsu:") else "movies" if type_ == "movie" else "shows",
                                   "season": season or 0, "number": number or 0})
                live = next(j for j in jobs.jobs if j["id"] == got["id"])  # the live one, its state changes
            except (ValueError, OSError, KeyError) as err:
                stand[sid].update(state="failed", error=str(err)[:200])
                continue
            with _lock:
                if _running.get(base + sid) is stand[sid]:
                    _running[base + sid] = live
    threading.Thread(target=run, daemon=True).start()


def _ready(tv, lib: str, key: str) -> str | None:
    eid = _done().get(key)
    if eid in tv.episodes:
        return eid
    job = _running.get(key)
    if job and (eid := _made(tv, lib, job)):
        DONE.write_text(json.dumps({**_done(), key: eid}), encoding="utf-8")
        return eid
    return None


def prepare(tv, body: dict) -> dict:
    person, lang = str(body.get("person", "")), str(body.get("lang", ""))
    type_, sid, season = str(body.get("type", "")), str(body.get("id", "")), body.get("season") == "1"
    q = str(body.get("q") or "1080p")
    lib = sync.PEOPLE.get(person, {}).get(lang)
    if not lib or type_ not in ("movie", "series") or not ID.match(sid) or q not in QUALITIES:
        raise ValueError("bad prepare request")
    base = f"{person}|{lang}|"
    key = base + sid
    with _lock:
        tv.synced()
        if eid := _ready(tv, lib, key):
            answer = {"state": "ready", "id": eid}
        else:
            job = _running.get(key)
            if job and job["state"] in ("failed", "done"):  # reported once; picking it again starts over
                del _running[key]
                if job["state"] == "failed":
                    return {"state": "failed", "step": "", "error": job["error"]}
            if key not in _running:
                _queue(base, person, lang, type_, [sid], q)
            job = _running[key]
            answer = {"state": job["state"], "step": job["step"], "error": job["error"]}
        if type_ == "series":
            try:
                eps = meta(type_, sid)["episodes"]
            except (OSError, ValueError):
                eps = []
            ids = [e[0] for e in eps]
            at = ids.index(sid) if sid in ids else -1
            if season:  # the rest of its season (Kitsu: of the title), in order
                more = [e[0] for e in eps if e[1] == numbers(sid)[0] and e[0] != sid]
            else:  # ready by the time this one ends, the next season's first ones too
                more = ids[at + 1:at + 1 + AHEAD] if at >= 0 else []
            more = [s for s in more if base + s not in _running and _done().get(base + s) not in tv.episodes]
            if more:
                _queue(base, person, lang, type_, more, q)
        return answer


if __name__ == "__main__":
    t = [{"name": "[RD+] Torrentio\n4k", "title": "x\n💾 12.1 GB", "url": "a"},
         {"name": "[RD+] Torrentio\n1080p", "title": "y\n💾 2.3 GB", "url": "b"},
         {"name": "[RD+] Torrentio\n1080p", "title": "z\n💾 900 MB", "url": "c"},
         {"name": "[RD download] Torrentio\n1080p", "title": "w\n💾 100 MB", "url": "d"},
         {"name": "[RD+] Torrentio\n1080p", "title": "Show S01 BluRay\n💾 1.1 GB",
          "url": "https://t/resolve/realdebrid/KEY/" + "a" * 40 + "/null/0/x.mkv"}]
    assert pick(t)["url"] == "c" and pick(t, "4k")["url"] == "a" and pick(t, "720p")["url"] == "c"
    assert pick(t, torrent="a" * 40)["url"].startswith("https://t/")  # the season's torrent first
    assert qualities(t[0]) == {"4k"} and qualities(t[4]) == {"1080p", "BD"}
    assert ID.match("kitsu:43248:1") and ID.match("tt0903747:1:2") and ID.match("tt0111161")
    assert not ID.match("tt1/../x")
    assert numbers("tt0903747:1:2") == (1, 2) and numbers("kitsu:43248:5") == (1, 5) and numbers("tt1") == (None, None)
    print("ok")
