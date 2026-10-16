"""NuvioTV (or any Stremio app) -> a kumapie episode made on the PC (kumapie_plan.md item 5a).

A Stremio add-on per person: every movie or episode gets one stream per language of that person's libraries (anime,
Kitsu ids: Japanese only), whose only link is kumapie://prepare?... NuvioTV opens such a link straight in kumapie
(VIEW + BROWSABLE, no "Open with" menu, which hides kumapie on the TCL TV). kumapie then asks the PC to prepare it:
Torrentio finds a Real-Debrid file, jobs.py makes the library episode (its own subtitles, else Jimaku, else
Parakeet), and kumapie plays it from the PC like any other.

  GET  /addon/<person>/manifest.json            the add-on (install: stremio://<host>/addon/<person>/manifest.json)
  GET  /addon/<person>/stream/<type>/<id>.json  {"streams": [{"name", "title", "externalUrl": "kumapie://prepare?..."}]}
  POST /api/tv/prepare  {"person", "lang", "type", "id"} -> {"state": "ready", "id": episode} or
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
_lock = threading.Lock()
_running: dict[str, dict] = {}  # key -> its jobs.py job


def manifest(person: str) -> dict:
    return {"id": f"dojo.kumapie.{person}", "version": "1.0.0", "name": "kumapie",
            "description": "Watch it in kumapie: the PC makes the episode first (subtitles, words).",
            "resources": ["stream"], "types": ["movie", "series"], "idPrefixes": ["tt", "kitsu"], "catalogs": []}


def streams(person: str, type_: str, sid: str) -> dict:
    langs = ["ja"] if sid.startswith("kitsu:") else list(sync.PEOPLE[person])
    return {"streams": [{"name": "kumapie", "title": f"{NAMES.get(lang, lang)}: prepare on the PC",
                         "externalUrl": "kumapie://prepare?" + urllib.parse.urlencode(
                             {"person": person, "lang": lang, "type": type_, "id": sid})}
                        for lang in langs if lang in sync.PEOPLE[person]]}


def _get(url: str, **kw):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "kumapie"}), timeout=30, **kw)


def _size(title: str) -> float:
    m = SIZE.search(title)
    return float(m.group(1)) * {"KB": 1e-6, "MB": 1e-3, "GB": 1, "TB": 1e3}[m.group(2)] if m else 1e9


def pick(found: list[dict]) -> dict:
    """Torrentio's streams -> the one to download: cached on Real-Debrid, 1080p, smallest (the PC downloads it all)."""
    cached = [s for s in found if "RD+" in s.get("name", "")] or found
    hd = [s for s in cached if "1080p" in s.get("name", "")] or cached
    return min(hd, key=lambda s: _size(s.get("title", "")))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _rd_link(type_: str, sid: str, token: str) -> str:
    """The Real-Debrid download link of the best Torrentio file for this movie or episode."""
    with _get(f"{TORRENTIO}/realdebrid={token}/stream/{type_}/{urllib.parse.quote(sid)}.json") as r:
        found = json.load(r).get("streams", [])
    if not found:
        raise ValueError("Torrentio found nothing for this")
    # Torrentio's resolve link answers 302 to the file on Real-Debrid's servers.
    try:
        urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(pick(found)["url"], headers={"User-Agent": "kumapie"}), timeout=60)
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


def prepare(tv, body: dict) -> dict:
    person, lang = str(body.get("person", "")), str(body.get("lang", ""))
    type_, sid = str(body.get("type", "")), str(body.get("id", ""))
    lib = sync.PEOPLE.get(person, {}).get(lang)
    if not lib or type_ not in ("movie", "series") or not ID.match(sid):
        raise ValueError("bad prepare request")
    key = f"{person}|{lang}|{sid}"
    with _lock:
        eid = _done().get(key)
        tv.synced()
        if eid in tv.episodes:
            return {"state": "ready", "id": eid}
        job = _running.get(key)
        if job and job["state"] == "done":
            tv.synced(force=True)
            stems = set(job["episodes"])
            eid = next((e["id"] for e in tv.episodes.values()
                        if e["person"] == lib and isinstance(e.get("path"), Path) and e["path"].stem in stems), None)
            if not eid:
                raise ValueError("the episode was made but isn't listed")
            DONE.write_text(json.dumps({**_done(), key: eid}), encoding="utf-8")
            del _running[key]
            return {"state": "ready", "id": eid}
        if not job or job["state"] == "failed" and body.get("retry"):
            link = _rd_link(type_, sid, os.environ.get("RD_API_TOKEN", ""))
            job = jobs.submit({"person": person, "lang": lang, "url": link, "rd": os.environ.get("RD_API_TOKEN", ""),
                               "jimaku": os.environ.get("JIMAKU_API_KEY", "") if lang == "ja" else "",
                               "type": "anime" if sid.startswith("kitsu:") else "movies" if type_ == "movie" else "shows"})
            job = next(j for j in jobs.jobs if j["id"] == job["id"])  # the live one, its state changes
            _running[key] = job
        return {"state": job["state"], "step": job["step"], "error": job["error"]}


if __name__ == "__main__":
    t = [{"name": "[RD+] Torrentio\n4k", "title": "x\n💾 12.1 GB", "url": "a"},
         {"name": "[RD+] Torrentio\n1080p", "title": "y\n💾 2.3 GB", "url": "b"},
         {"name": "[RD+] Torrentio\n1080p", "title": "z\n💾 900 MB", "url": "c"},
         {"name": "[RD download] Torrentio\n1080p", "title": "w\n💾 100 MB", "url": "d"}]
    assert pick(t)["url"] == "c", pick(t)
    assert ID.match("kitsu:43248:1") and ID.match("tt0903747:1:2") and ID.match("tt0111161")
    assert not ID.match("tt1/../x")
    print("ok")
