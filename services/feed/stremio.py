"""NuvioTV (or any Stremio app) -> a kumapie episode made on the PC (kumapie_plan.md item 5a).

A Stremio add-on per person: every movie or episode gets the best three files per quality that Torrentio and Comet
know, each as "this one" / "whole season" per language of that person's libraries (anime, Kitsu ids: Japanese only).
Best = the highest bitrate (Pedro watches on a 4K TV), files the PC must re-encode (AV1: the TV can't play it)
last. Each entry's only link is kumapie://prepare?...&hash&file&idx. NuvioTV opens such a link straight in kumapie
(VIEW + BROWSABLE, no "Open with" menu, which hides kumapie on the TCL TV). kumapie then asks the PC to prepare it: the
PC adds that torrent's file to Real-Debrid (waiting while Real-Debrid downloads one it doesn't have yet), jobs.py makes
the library episode (its own subtitles, else Jimaku, else Parakeet), and kumapie plays it from the PC like any other.
Comet is asked without a debrid key, so the Real-Debrid token only goes to Torrentio (for its cache flags) and
Real-Debrid. Episodes are named from Stremio's catalog (Cinemeta, Kitsu), not from file names: show, season, number. A
season queues each of its episodes (the picked one first, the rest from the same torrent when it has them); opening an
episode queues the next two (into the next season too).

  GET  /addon/<person>/manifest.json            the add-on (install: stremio://<host>/addon/<person>/manifest.json)
  GET  /addon/<person>/stream/<type>/<id>.json  {"streams": [{"name", "title", "externalUrl": "kumapie://prepare?..."}]}
  POST /api/tv/prepare  {"person", "lang", "type", "id", "q"?, "season"?: "1", "hash"?, "file"?, "idx"?} ->
                        {"state": "ready", "id": episode} or {"state": "queued" | "running" | "failed", "step", "error"}:
                        ask again until ready
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dojo import cfg
import jobs
import realdebrid  # the phone's module, on the path through jobs.py
import sync

TORRENTIO = "https://torrentio.strem.fun"
COMET = "https://comet.feels.legal"  # a public Comet that answers without a debrid key (ElfHosted's doesn't)
META = {"tt": "https://v3-cinemeta.strem.io", "kitsu": "https://anime-kitsu.strem.fun"}
DONE = cfg.state / "feed" / "stremio.json"  # {"<person>|<lang>|<id>": episode id}: asking again is instant
NAMES = {"de": "Deutsch", "en": "English", "ja": "日本語"}
ID = re.compile(r"^(tt\d+|kitsu:\d+)(:\d+:\d+|:\d+)?$")
HASH = re.compile(r"^[0-9a-f]{40}$")
SIZE = re.compile(r"💾\s*([\d.]+)\s*([KMGT]B)")
TORRENTIO_FILE = re.compile(r"/realdebrid/[^/]+/([0-9a-fA-F]{40})/[^/]+/([^/]+)/")  # .../<hash>/<cached>/<fileIdx>/
QUALITIES = ["BD", "4k", "2k", "1080p", "720p", "480p"]  # the add-on's order
RESOLUTION = {"4k": r"\b(4k|2160p)\b", "2k": r"\b(2k|1440p)\b", "1080p": r"\b1080p\b", "720p": r"\b720p\b",
              "480p": r"\b(480p|576p|sd)\b"}
OPTIONS = 3  # entries per quality, best first
MINUTES = {"movie": 110, "series": 45}  # when the catalog has no runtime
# Not the episode, or not watchable on a TV (3D side-by-side / over-under).
JUNK = re.compile(r"\bnc ?(op|ed)|creditless|\b(op|ed|pv|cm)[ ._-]?\d*\b|preview|trailer|sample|menu"
                  r"|\b(full-?|half-?|h-?)?sbs\b|\b(full-?|half-?|h-?)ou\b", re.I)
HEIGHT = {"4k": 4, "2k": 3, "1080p": 2, "720p": 1, "480p": 0}
DEAD = ("error", "magnet_error", "virus", "dead")  # Real-Debrid torrent states that never finish
TAGS = [("AV1", r"\bav1\b"), ("HEVC", r"\b(hevc|x265|h\.?265)\b"), ("AVC", r"\b(avc|x264|h\.?264)\b"),
        ("10bit", r"10-?bit|hi10"), ("WEB", r"\bweb(-?dl|-?rip)?\b"), ("Remux", r"remux"),
        ("FLAC", r"flac"), ("Opus", r"\bopus\b"), ("DD+", r"e-?ac-?3|\bddp|dd\+"), ("AC3", r"(?<![e-])\bac-?3\b"),
        ("AAC", r"\baac\b"), ("DTS", r"\bdts\b"), ("Dual Audio", r"dual[ ._-]?audio"), ("Multi Subs", r"multi[ ._-]?subs")]
AHEAD = 2  # episodes queued after the one being watched
_lock = threading.Lock()
_running: dict[str, dict] = {}  # key -> its jobs.py job (or a stand-in until it's queued)
_metas: dict[str, dict] = {}


def manifest(person: str) -> dict:
    return {"id": f"dojo.kumapie.{person}", "version": "1.3.0", "name": "kumapie",
            "description": "Watch it in kumapie: the PC makes the episode first (subtitles, words).",
            "resources": ["stream"], "types": ["movie", "series"], "idPrefixes": ["tt", "kitsu"], "catalogs": []}


def streams(person: str, type_: str, sid: str) -> dict:
    """The best OPTIONS files per quality, each per language and episode / whole season."""
    langs = [lang for lang in (["ja"] if sid.startswith("kitsu:") else sync.PEOPLE[person]) if lang in sync.PEOPLE[person]]
    seasons = [False, True] if type_ == "series" else [False]
    with ThreadPoolExecutor(2) as pool:  # the catalog and the two indexers at once: NuvioTV waits for all of it
        minutes = pool.submit(_minutes, type_, sid)
        found = candidates(type_, sid, os.environ.get("RD_API_TOKEN", ""))
        minutes = minutes.result()
    out = []
    for q in QUALITIES:
        best = {}  # a file name once (a season pack and the episode's own torrent)
        for c in ranked([c for c in found if q in qualities(c)], minutes):
            best.setdefault(c["file"], c)
        for c in list(best.values())[:OPTIONS]:
            gb, mbps, redo = c["gb"], _mbps(c, minutes), rank(c, minutes)[0]
            size = f"{gb:.1f} GB" if gb >= 1 else f"{gb * 1000:.0f} MB"
            if q == "BD":  # which resolution the Blu-ray file is
                size = " · ".join(sorted(qualities(c) - {"BD"}) + [size])
            notes = ("" if c["cached"] else " · ⏳ Real-Debrid downloads it first" if c["cached"] is False
                     else " · ⏳ maybe not on Real-Debrid yet") + (" · 🔄 the PC re-encodes it" if redo else "")
            for season in seasons:
                for lang in langs:
                    out.append({"name": f"kumapie\n{q}",
                                "title": f"{NAMES.get(lang, lang)} · {'whole season' if season else 'this one'} · "
                                         f"💾 {size}{' an episode' if season else ''} · {mbps:.1f} Mbps{notes}{about(c)}",
                                "externalUrl": "kumapie://prepare?" + urllib.parse.urlencode(
                                    {"person": person, "lang": lang, "type": type_, "id": sid, "q": q,
                                     "hash": c["hash"], "file": c["file"], "idx": c["idx"],
                                     **({"season": "1"} if season else {})})})
    return {"streams": out}


def candidates(type_: str, sid: str, token: str) -> list[dict]:
    """Every file Torrentio (with Real-Debrid's cache) and Comet know for this title, once each: {"hash", "idx", "file",
    "gb", "res", "text", "cached": True / False / None (unknown), "seeders", "site", "langs"}. One down: the other's."""
    path = f"/stream/{type_}/{urllib.parse.quote(sid)}.json"
    with ThreadPoolExecutor(2) as pool:
        tor, com = pool.map(_streams, [f"{TORRENTIO}/realdebrid={token}{path}", COMET + path])
    out: dict[tuple, dict] = {}
    for s in tor:
        lines = s.get("title", "").split("\n")
        stats = next((i for i, line in enumerate(lines) if line.startswith("👤")), len(lines))
        file = (s.get("behaviorHints") or {}).get("filename") or (lines[stats - 1] if stats else "")
        m, st = TORRENTIO_FILE.search(s.get("url", "")), lines[stats] if stats < len(lines) else ""
        if m and file:
            out[(m[1].lower(), file)] = {
                "hash": m[1].lower(), "idx": int(m[2]) if m[2].isdigit() else -1, "file": file, "gb": _size(st),
                "res": s.get("name", ""), "text": " ".join(lines[:stats]), "cached": "RD+" in s.get("name", ""),
                "seeders": _find(r"👤\s*(\d+)", st), "site": _find(r"⚙️\s*(.+)", st),
                "langs": lines[stats + 1] if stats + 1 < len(lines) else ""}
    # Comet also lists torrent names without a file, books, other shows' S01E01, other episodes: only a video file of
    # this show (a name from the catalog or Torrentio's files in its name) and episode (the id's number or one at least
    # two of Torrentio's files have: absolute numbers, S2E1 = "- 25").
    try:
        about = meta(type_, sid)
    except (OSError, ValueError):
        about = {"names": []}
    shows = {n for n in map(_plain, about["names"] + [realdebrid.show_name(c["file"]) for c in out.values()]) if len(n) >= 4}
    season, number = numbers(sid)
    seen = Counter(realdebrid.numbers(c["file"])[1] for c in out.values())
    known = {number} | {n for n, k in seen.items() if k >= 2}

    def right(file: str) -> bool:
        s, n = realdebrid.numbers(file)
        return any(n in _plain(file, False) for n in shows) and (
            number is None or n in known and (s is None or s == season or sid.startswith("kitsu:")))
    for s in com:
        h, hints, idx = str(s.get("infoHash") or "").lower(), s.get("behaviorHints") or {}, s.get("fileIdx")
        file, text = hints.get("filename") or "", s.get("description", "")
        if HASH.match(h) and isinstance(idx, int) and file.lower().endswith(realdebrid.VIDEO_EXT) and right(file) \
                and (h, file) not in out:
            out[(h, file)] = {"hash": h, "idx": idx, "file": file,
                              "gb": (hints.get("videoSize") or 0) / 1e9 or _size(text), "res": s.get("name", ""),
                              "text": text, "cached": None, "seeders": None,
                              "site": "Comet" + (f" · {w}" if (w := _find(r"🔎\s*(.+)", text)) else ""), "langs": ""}
    return [c for c in out.values() if not JUNK.search(c["file"]) and c["gb"] >= 0.03]


def _plain(name: str, season: bool = True) -> str:
    """A show's name to compare with a file's: letters and digits only, no leading "the", no "2nd Season" / "Season 2"
    ([season] False: a file name, left whole)."""
    name = name.lower()
    if season:
        name = re.sub(r"\b(season|part|cour)\s*\d+|\d+(st|nd|rd|th)\s*season|\bs\d+\b|\(\d{4}\)", " ", name)
        name = re.sub(r"^\W*the\b", "", name)
    return re.sub(r"[\W_]+", "", name)


def tags(c: dict) -> list[str]:
    words = f"{c['text']} {c['file']}".lower()
    return [t for t, pat in TAGS if re.search(pat, words)]


def about(c: dict) -> str:
    """An entry's lines after the first: codec / audio tags, seeders and site, the torrent's languages, the file."""
    out = [" · ".join(tags(c)),
           " · ".join(x for x in [f"👤 {c['seeders']}" if c["seeders"] else "", f"⚙️ {c['site']}" if c["site"] else ""] if x),
           f"🌐 {c['langs']}" if c["langs"] else "", f"📄 {c['file']}"]
    return "".join("\n" + x for x in out if x)


def qualities(c: dict) -> set[str]:
    """A file's qualities: its resolution (from the add-on's name), plus BD for a Blu-ray source."""
    got = {q for q, pat in RESOLUTION.items() if re.search(pat, c["res"].lower())}
    if re.search(r"blu-?ray|\bbd(rip|remux)?\b|remux", f"{c['text']} {c['file']}".lower()):
        got.add("BD")
    return got


def _mbps(c: dict, minutes: float) -> float:
    return c["gb"] * 8000 / (minutes * 60)


def rank(c: dict, minutes: float) -> tuple:
    """Smaller is better: first a file jobs._remux copies as it is (H.264 / HEVC at any bitrate; a codec the name
    doesn't say is most likely H.264), not one it re-encodes (AV1); then the highest resolution (Blu-rays come in
    several); then not a file Real-Debrid is known not to have; then the highest bitrate."""
    height = max((HEIGHT[q] for q in qualities(c) if q in HEIGHT), default=-1)
    return "AV1" in tags(c), -height, c["cached"] is False, -_mbps(c, minutes)


def ranked(found: list[dict], minutes: float, torrent: str = "") -> list[dict]:
    """[found] best first; the files of [torrent] (a season's episodes from one release) before all others."""
    return sorted(found, key=lambda c: (c["hash"] != torrent, rank(c, minutes)))


def _find(pat: str, text: str) -> str | None:
    m = re.search(pat, text)
    return m[1].strip() if m else None


def _get(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "kumapie"}), timeout=30)


def _streams(url: str) -> list[dict]:
    try:
        with _get(url) as r:
            return json.load(r).get("streams", [])
    except (OSError, ValueError):  # that indexer down: the other one's entries rather than an error in NuvioTV
        return []


def _size(text: str) -> float:
    m = SIZE.search(text)
    return float(m.group(1)) * {"KB": 1e-6, "MB": 1e-3, "GB": 1, "TB": 1e3}[m.group(2)] if m else 0


def _file_of(info: dict, file: str, idx: int) -> dict | None:
    """The file [file] (by name, else the torrent's file [idx]: Real-Debrid numbers them from 1) of a torrent's info."""
    return next((f for f in info["files"] if os.path.basename(f["path"]) == file), None) or \
        next((f for f in info["files"] if f["id"] == idx + 1), None) if idx >= 0 or file else None


def _rd_link(token: str, h: str, file: str, idx: int, step) -> str:
    """The Real-Debrid link of one file of torrent [h]: from a torrent the account already has with it picked, else
    the magnet added with only that file; waits while Real-Debrid downloads it ([step] gets the progress)."""
    step("asking Real-Debrid")
    tid, f = None, None
    for t in realdebrid._call(token, "GET", "/torrents?limit=100") or []:
        if t["hash"].lower() == h and t["status"] not in DEAD:
            info = realdebrid._call(token, "GET", f"/torrents/info/{t['id']}")
            if (f := _file_of(info, file, idx)) and f["selected"]:
                tid = t["id"]
                break
    if not tid:
        tid = realdebrid._call(token, "POST", "/torrents/addMagnet", {"magnet": f"magnet:?xt=urn:btih:{h}"})["id"]
        while (info := realdebrid._call(token, "GET", f"/torrents/info/{tid}"))["status"] == "magnet_conversion":
            time.sleep(3)
        if not (f := _file_of(info, file, idx)):
            raise ValueError(f"Real-Debrid found no {file} in that torrent")
        realdebrid._call(token, "POST", f"/torrents/selectFiles/{tid}", {"files": str(f["id"])})
    progress, since = -1, time.time()
    while (info := realdebrid._call(token, "GET", f"/torrents/info/{tid}"))["status"] != "downloaded":
        if info["status"] in DEAD:
            raise ValueError(f"Real-Debrid couldn't get it ({info['status']}): pick another one")
        if info.get("progress", 0) != progress:
            progress, since = info.get("progress", 0), time.time()
        elif time.time() - since > 1800:
            raise ValueError("Real-Debrid got nothing more of it in 30 minutes (nobody shares it?): pick another one")
        step(f"Real-Debrid is downloading it: {progress}% ({info.get('seeders') or 0} seeders)"
             if info["status"] == "downloading" else f"Real-Debrid: {info['status'].replace('_', ' ')}")
        time.sleep(10)
    picked = sorted(x["id"] for x in info["files"] if x["selected"])
    if len(picked) != len(info["links"]):  # Real-Debrid packed several files into one archive
        raise ValueError("Real-Debrid gave no link of its own for that file")
    return info["links"][picked.index(f["id"])]


def meta(type_: str, sid: str) -> dict:
    """The title in Stremio's catalog: {"name", "minutes" (0: unknown), "episodes": [[id, season, number]] in order}
    (specials left out). Kitsu has a title per season, numbered from 1, so its season is 1; a movie has no episodes."""
    base = ":".join(sid.split(":")[:2 if sid.startswith("kitsu:") else 1])
    if base not in _metas:
        with _get(f"{META['kitsu' if base.startswith('kitsu:') else 'tt']}/meta/{type_}/{base}.json") as r:
            m = json.load(r).get("meta") or {}
        eps = sorted(([v["id"], v.get("season") or 1 if base.startswith("tt") else 1, v.get("episode") or v.get("number")]
                      for v in m.get("videos") or [] if ID.match(v.get("id", "")) and v.get("season", 1) != 0),
                     key=lambda e: (e[1], e[2] or 0))
        run = str(m.get("runtime") or next((v.get("runtime") for v in m.get("videos") or [] if v.get("runtime")), ""))
        h, mins = re.search(r"(\d+)\s*h", run), re.search(r"(\d+)\s*m", run) or re.fullmatch(r"\s*(\d+)\s*", run)
        _metas[base] = {"name": m.get("name") or base, "episodes": eps, "names": [m.get("name") or ""] + (m.get("aliases") or []),
                        "minutes": (int(h[1]) * 60 if h else 0) + (int(mins[1]) if mins else 0)}
    return _metas[base]


def _minutes(type_: str, sid: str) -> float:
    try:
        return meta(type_, sid)["minutes"] or MINUTES[type_]
    except (OSError, ValueError):
        return MINUTES[type_]


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


def _queue(base: str, person: str, lang: str, type_: str, sids: list[str], q: str, first: dict | None = None,
           torrent: str = "", replace: bool = False) -> None:
    """Queues one job per id, in order: [first] (the picked file) for the first, the others from [torrent] (then the
    first's) where it has them, else the best of quality [q] (in the background: the indexers and Real-Debrid take
    seconds, a file Real-Debrid doesn't have yet minutes); stand-ins answer until then. [replace]: an episode already
    in the library is made again from that file (jobs.py archives the old one)."""
    if not sids or base + sids[0] in _running:
        first = None
    sids = [s for s in sids if base + s not in _running]
    stand = {s: {"state": "queued", "step": "asking Real-Debrid", "error": "", "episodes": []} for s in sids}
    _running.update({base + s: j for s, j in stand.items()})

    def run():
        token, last = os.environ.get("RD_API_TOKEN", ""), torrent
        for i, sid in enumerate(sids):
            def step(text, sid=sid):
                stand[sid]["step"] = text
            try:
                about_ = meta(type_, sid) if type_ == "series" else {"name": ""}
                season, number = numbers(sid)
                if i == 0 and first:
                    c = first
                else:
                    step("asking Torrentio and Comet")
                    found = [c for c in candidates(type_, sid, token) if q in qualities(c) or c["hash"] == last]
                    if not found:
                        raise ValueError(f"Torrentio and Comet found no {q} file for this")
                    c = ranked(found, _minutes(type_, sid), last)[0]
                link = _rd_link(token, c["hash"], c["file"], c["idx"], step)
                last = c["hash"]
                got = jobs.submit({"person": person, "lang": lang, "url": link, "rd": token, "show": about_["name"],
                                   "jimaku": os.environ.get("JIMAKU_API_KEY", "") if lang == "ja" else "",
                                   "type": "anime" if sid.startswith("kitsu:") else "movies" if type_ == "movie" else "shows",
                                   "season": season or 0, "number": number or 0,
                                   "replace": replace, "pick": f"{c['hash']}|{c['file']}"})
                live = next(j for j in jobs.jobs if j["id"] == got["id"])  # the live one, its state changes
            except (ValueError, OSError, KeyError, RuntimeError) as err:  # RuntimeError: Real-Debrid's answer
                stand[sid].update(state="failed", error=str(err)[:200])
                continue
            with _lock:
                if _running.get(base + sid) is stand[sid]:
                    _running[base + sid] = live
    threading.Thread(target=run, daemon=True).start()


def _ready(tv, lib: str, key: str, want: str = "") -> str | None:
    """The episode of [key] if it's in the library, made from file [want] ("hash|name") when that's given. DONE also
    keeps "from|<key>": the file each episode was made from."""
    job = _running.get(key)
    if job and (eid := _made(tv, lib, job)):  # made (or found there) by a job: remember which file it's from
        del _running[key]
        DONE.write_text(json.dumps({**_done(), key: eid, **({"from|" + key: job["pick"]} if job.get("pick") else {})}),
                        encoding="utf-8")
    done = _done()
    eid = done.get(key)
    return eid if eid in tv.episodes and (not want or done.get("from|" + key) == want) else None


def prepare(tv, body: dict) -> dict:
    person, lang = str(body.get("person", "")), str(body.get("lang", ""))
    type_, sid, season = str(body.get("type", "")), str(body.get("id", "")), body.get("season") == "1"
    q = str(body.get("q") or "1080p")
    h, file, idx = str(body.get("hash") or "").lower(), str(body.get("file") or ""), str(body.get("idx") or "-1")
    first = {"hash": h, "file": file, "idx": int(idx)} if HASH.match(h) and file and re.fullmatch(r"-?\d+", idx) else None
    lib = sync.PEOPLE.get(person, {}).get(lang)
    if not lib or type_ not in ("movie", "series") or not ID.match(sid) or q not in QUALITIES:
        raise ValueError("bad prepare request")
    base = f"{person}|{lang}|"
    key = base + sid
    # A picked file is a wish for that very file: an episode made from another one (or before this was recorded) is
    # made again from it, the old one archived.
    want = f"{first['hash']}|{first['file']}" if first else ""
    with _lock:
        tv.synced()
        if eid := _ready(tv, lib, key, want):
            answer = {"state": "ready", "id": eid}
        else:
            job = _running.get(key)
            if job and job["state"] in ("failed", "done"):  # reported once; picking it again starts over
                del _running[key]
                if job["state"] == "failed":
                    return {"state": "failed", "step": "", "error": job["error"]}
            if key not in _running:
                _queue(base, person, lang, type_, [sid], q, first, replace=bool(first))
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
            # A whole season picked from one release replaces the season's other episodes not made from it either.
            again = bool(season and first)
            done = _done()
            more = [s for s in more if base + s not in _running and not (
                done.get(base + s) in tv.episodes
                and (not again or done.get("from|" + base + s, "").startswith(first["hash"] + "|")))]
            if more:
                _queue(base, person, lang, type_, more, q, torrent=first["hash"] if first else "", replace=again)
        return answer


if __name__ == "__main__":
    def c(file, gb, res="1080p", text="", cached=True, h="a"):
        return {"hash": h * 40, "idx": 0, "file": file, "gb": gb, "res": res, "text": text, "cached": cached,
                "seeders": None, "site": None, "langs": ""}
    av1, hevc = c("x (BD 1080p AV1 Opus).mkv", 0.46), c("y (BD 1080p HEVC Opus).mkv", 1.0, h="b")
    big_hevc, remux = c("z HEVC 10bit.mkv", 1.9, h="c", cached=None), c("w BD Remux AVC.mkv", 6.2, h="d")
    plain, old = c("[SubsPlease] Show - 25 (1080p).mkv", 1.36, h="e"), c("v x264.mkv", 2.0, h="f", cached=False)
    order = [x["file"] for x in ranked([av1, hevc, big_hevc, remux, plain, old], 24)]
    # highest bitrate first (a remux too), the one Real-Debrid lacks after those, the re-encoded AV1 last
    assert order == [remux["file"], big_hevc["file"], plain["file"], hevc["file"], old["file"], av1["file"]], order
    assert ranked([av1, hevc], 24, torrent="a" * 40)[0] is av1  # a season's torrent first
    bd720, bd1080 = c("a BDRip.mkv", 1.2, res="720p", h="g"), c("b Bluray.mkv", 0.9, h="h")
    assert ranked([bd720, bd1080], 24)[0] is bd1080  # the higher resolution before a higher bitrate
    assert JUNK.search("Breaking.Bad.S01E02.1080p.3D.FULL-SBS.HEVC.mkv") and JUNK.search("Movie.2010.3D.H-OU.mkv")
    assert not JUNK.search("3D Kanojo - 01.mkv") and not JUNK.search("Tu ou moi - 01.mkv")
    assert qualities(av1) == {"1080p", "BD"} and qualities(c("a.mkv", 1, res="Comet 2160p")) == {"4k"}
    assert JUNK.search("[Shiniori-Raws] Show S2 - NCOP 01 (BD 2160p).mkv") and not JUNK.search("Show 01 [Opus].mkv")
    assert _plain("Kusuriya no Hitorigoto 2nd Season") in _plain("[x] Kusuriya no Hitorigoto - 25.mkv", False)
    assert _plain("The Apothecary Diaries Season 2") in _plain("The.Apothecary.Diaries.S02E01.mkv", False)
    assert _plain("Lucky☆Star") in _plain("Lucky.Star.S01E01.1080p.mkv", False)
    assert _plain("Lucky☆Star") not in _plain("Monthly.Girls.Nozaki-kun.S01E01.mkv", False)
    assert ID.match("kitsu:43248:1") and ID.match("tt0903747:1:2") and ID.match("tt0111161")
    assert not ID.match("tt1/../x")
    assert numbers("tt0903747:1:2") == (1, 2) and numbers("kitsu:43248:5") == (1, 5) and numbers("tt1") == (None, None)
    pi = {**c("X - S02E01 (Bluray-1080p AV1 Opus)-Pi13.mkv", 0.46, text="The.Apothecary.Diaries.S02.1080p.BluRay"
                                                                          ".Dual-Audio.Opus.2.0.AV1-Pi13"),
          "seeders": "87", "site": "NyaaSi", "langs": "Dubbed / Dual Audio / 🇬🇧"}
    assert about(pi) == ("\nAV1 · Opus · Dual Audio\n👤 87 · ⚙️ NyaaSi\n🌐 Dubbed / Dual Audio / 🇬🇧"
                         "\n📄 X - S02E01 (Bluray-1080p AV1 Opus)-Pi13.mkv"), about(pi)
    info = {"files": [{"id": 1, "path": "/S/a.mkv"}, {"id": 2, "path": "/S/b.mkv"}]}
    assert _file_of(info, "b.mkv", 0)["id"] == 2 and _file_of(info, "", 0)["id"] == 1 and not _file_of(info, "c.mkv", -1)
    print("ok")
