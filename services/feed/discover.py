"""kumapie's Discover page: browse Stremio's catalog, pick a Torrentio + Real-Debrid file, watch it streamed
(kumapie_plan.md item 5b). Japanese first: Kitsu's catalog (the anime add-on Stremio uses).

The video never lands on the PC: the phone or TV plays Real-Debrid's link itself. The PC only makes the episode's words:
a Jimaku file made for this very release (same group / CRC, or Crunchyroll's WEB timing), used as it is; else it reads
the file once over the network for its Japanese subtitle track, else its audio + English track to fit Jimaku's
subtitles (jobs._jimaku: alass against the English track or a Parakeet transcript), then tv.stream() splits them like
any NuvioTV stream (a stream-<hex> episode, not listed in Shows). Asking again for the same file is instant.
The video plays as soon as Real-Debrid's link exists ("video"); the words follow ("ready"). Once ready, the next
episode is got ready the same way, in the background.

  GET  /api/discover/catalog?q=<text>                 [{"id", "type", "name", "poster", "year"}] (no q: trending)
  GET  /api/discover/meta/<type>/<id>?person&lang     {"name", "poster", "background", "description", "year",
                                                       "episodes": [{"id", "season", "number", "title", "thumb",
                                                                     "ready": a file is prepared for it}]}
  GET  /api/discover/streams/<type>/<id>              [{"q", "label", "about", "hash", "file", "idx", "now"}] best first
                                                       (now: Real-Debrid has it, it starts at once; those first)
  POST /api/tv/watch {"person", "lang", "type", "id", "hash", "file", "idx", "title"?}
       -> {"state": "video", "url", "step"} (play it, words coming) / {"state": "ready", "id": episode, "url": video,
          "next"?: next episode's id} / {"state": "running" | "failed", "step", "error"}
       No hash/file/idx: the file prepared for that episode, {"state": "none"} if there is none.
  GET  /api/discover/board?person                     Stremio's board: [{"id", "name", "items": [title]}], the
                                                       person's AniList Planning list first, then Kitsu's catalogs
                                                       (title: {"id", "type", "name", "poster", "year", "background",
                                                       "logo", "description", "genres", "rating", "runtime"})
  GET  /api/discover/subs/<type>/<id>?file           a Jimaku file's lines, shown live while the episode is made:
                                                       {"cues": [[start, end, text]], "name", "timed"}
  GET  /api/discover/saved?person                     the titles kept on Home: [{"id", "type", "name", "poster", "year"}]
  POST /api/discover/saved {"person", "id", "type", "name", "poster", "year", "on"}  keeps (on) / drops one -> the list
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dojo import cfg
import anilist
import jobs
import realdebrid
import stremio
import sync
from tv import STREAM, subtitle_cues

KITSU = stremio.META["kitsu"]
DONE = cfg.state / "feed" / "watch.json"  # {"<person>|<lang>|<hash>|<file>": stream episode id,
#                                            "ep|<person>|<lang>|<id>": {"hash", "file", "idx"} prepared for it}
PROBED = cfg.state / "feed" / "rd_probe.json"  # {"<hash>|<file>": [unix time, on Real-Debrid]}, trusted a day
KITSU_OF = cfg.state / "feed" / "anilist_kitsu.json"  # {AniList id: Kitsu id or ""}: Kitsu's mapping, for good
CATALOGS = [("airing", "Top airing"), ("trending", "Trending"), ("popular", "Most popular"), ("rating", "Highest rated")]
SAVED = cfg.state / "feed" / "discover_saved.json"  # {person: [{"id", "type", "name", "poster", "year"}]}: on Home
PROBES = 8 # files checked on Real-Debrid per list (~6 calls each; its limit is ~250 a minute)
TEXT_SUBS = {"subrip": "srt", "ass": "ass", "ssa": "ass", "webvtt": "vtt", "mov_text": "srt"}
LANGS = {"ja": ("ja", "jpn", "jp"), "en": ("en", "eng")}
WEB = re.compile(r"^\[(subsplease|erai-raws|horriblesubs|cr|crunchyroll)\]|\b(cr|crunchyroll)\b")  # Crunchyroll's timing
_lock = threading.Lock()
_running: dict[str, dict] = {}
_cache: dict[str, tuple[float, object]] = {}


def _json(url: str):
    with stremio._get(url) as r:
        return json.load(r)


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _done() -> dict:
    return _read(DONE)


def _remember(**more) -> None:
    with _lock:
        DONE.write_text(json.dumps({**_done(), **more}), encoding="utf-8")


def saved(person: str, body: dict | None = None) -> list[dict]:
    """A person's Discover titles kept on Home, newest first; [body] {"id", "type", "name", "poster", "year", "on"}
    adds (on true) or removes one first."""
    if person not in sync.PEOPLE:
        if body:
            raise ValueError("unknown person")
        return []
    with _lock:
        everyone = _read(SAVED)
        mine = [t for t in everyone.get(person, []) if not body or t["id"] != body.get("id")]
        if body and body.get("on"):
            if not stremio.ID.match(str(body.get("id", ""))) or body.get("type") not in ("movie", "series"):
                raise ValueError("bad title")
            mine.insert(0, {k: str(body.get(k) or "")[:300] for k in ("id", "type", "name", "poster", "year")})
        if body:
            SAVED.write_text(json.dumps({**everyone, person: mine}, ensure_ascii=False), encoding="utf-8")
    return mine


def _title(m: dict) -> dict:
    """A Stremio meta as Discover shows a title: poster for rows; backdrop, logo, rating, genres and text for the hero
    and the title page."""
    genres = m.get("genres") if isinstance(m.get("genres"), list) else []
    return {"id": m["id"], "type": "movie" if m.get("type") == "movie" else "series", "name": m.get("name", ""),
            "poster": m.get("poster", ""), "year": str(m.get("releaseInfo") or m.get("year") or ""),
            "background": m.get("background") or "", "logo": m.get("logo") or "", "description": m.get("description") or "",
            "genres": ", ".join(genres[:3]), "rating": str(m.get("imdbRating") or ""), "runtime": str(m.get("runtime") or "")}


def catalog(q: str = "") -> list[dict]:
    path = f"/catalog/anime/kitsu-anime-list/search={urllib.parse.quote(q)}.json" if q.strip() \
        else "/catalog/anime/kitsu-anime-trending.json"
    return [_title(m) for m in _json(KITSU + path).get("metas", []) if stremio.ID.match(m.get("id", ""))]


def _cached(key: str, hours: float, make):
    """make() kept [hours] in memory (the board's catalogs, AniList's list: seconds each to ask)."""
    got = _cache.get(key)
    if got and time.time() - got[0] < hours * 3600:
        return got[1]
    value = make()
    _cache[key] = (time.time(), value)
    return value


def _kitsu_of(anilist_ids: list[int]) -> dict[int, str]:
    """AniList id -> Kitsu id ("" when Kitsu has none), from Kitsu's own mapping table; kept in KITSU_OF for good."""
    known = _read(KITSU_OF)

    def one(aid):
        url = ("https://kitsu.io/api/edge/mappings?filter%5BexternalSite%5D=anilist/anime&filter%5BexternalId%5D="
               f"{aid}&include=item")
        try:
            data = _json(url).get("data") or []
            item = ((data[0].get("relationships") or {}).get("item") or {}).get("data") or {} if data else {}
            return aid, item.get("id", "") if item.get("type") == "anime" else ""
        except (OSError, ValueError):
            return aid, None  # asked again next time
    todo = [a for a in anilist_ids if str(a) not in known]
    with ThreadPoolExecutor(6) as pool:
        got = {str(a): k for a, k in pool.map(one, todo) if k is not None}
    if got:
        with _lock:
            KITSU_OF.write_text(json.dumps({**_read(KITSU_OF), **got}), encoding="utf-8")
        known.update(got)
    return {a: known.get(str(a), "") for a in anilist_ids}


def planning(person: str) -> list[dict]:
    """[person]'s AniList "Planning" list (anilist.py's user, for the person whose library it names), newest first,
    as Discover titles; [] for anyone else or without a user."""
    user, libs = anilist.CONF.get("user"), anilist.CONF.get("libraries", [])
    if not user or not any(lib in libs for lib in sync.PEOPLE.get(person, {}).values()):
        return []
    data = anilist.gql("""query ($user: String) { MediaListCollection(userName: $user, type: ANIME, status: PLANNING,
        sort: UPDATED_TIME_DESC) { lists { entries { mediaId } } } }""", {"user": user})
    ids = [e["mediaId"] for li in data["MediaListCollection"]["lists"] for e in li["entries"]]
    kitsu = _kitsu_of(ids)

    def one(aid):
        try:
            return _cached("meta|" + kitsu[aid], 24, lambda: _title(_json(f"{KITSU}/meta/series/kitsu:{kitsu[aid]}.json")["meta"]))
        except (OSError, ValueError, KeyError):
            return None
    with ThreadPoolExecutor(6) as pool:
        return [t for t in pool.map(one, [a for a in ids if kitsu.get(a)]) if t]


def board(person: str) -> list[dict]:
    """Discover's rows, Stremio's board: the person's AniList Planning list, then Kitsu's catalogs.
    [{"id", "name", "items": [title]}], rows that came back empty or failed left out."""
    rows = [("planning", "AniList · Planning", lambda: planning(person))] + [
        (c, name, lambda c=c: [_title(m) for m in _json(f"{KITSU}/catalog/anime/kitsu-anime-{c}.json").get("metas", [])
                               if stremio.ID.match(m.get("id", ""))]) for c, name in CATALOGS]

    def one(row):
        rid, name, make = row
        try:
            return {"id": rid, "name": name, "items": _cached(f"{rid}|{person}", 0.5, make)}
        except (OSError, ValueError, KeyError) as e:
            print(f"discover board {rid}: {e}", flush=True)
            return None
    with ThreadPoolExecutor(len(rows)) as pool:
        return [r for r in pool.map(one, rows) if r and r["items"]]


def meta(type_: str, sid: str, person: str = "", lang: str = "ja") -> dict:
    m = _json(f"{KITSU if sid.startswith('kitsu:') else stremio.META['tt']}/meta/{type_}/{sid}.json").get("meta") or {}
    done = _done()
    eps = [{"id": v["id"], "season": v.get("season") or 1, "number": v.get("episode") or v.get("number") or 0,
            "title": v.get("title") or v.get("name") or "", "thumb": v.get("thumbnail") or "",
            "ready": f"ep|{person}|{lang}|{v['id']}" in done}
           for v in m.get("videos") or [] if stremio.ID.match(v.get("id", "")) and v.get("season", 1) != 0]
    eps.sort(key=lambda e: (e["season"], e["number"]))
    return {**_title({"id": sid, **m}), "name": m.get("name") or sid, "episodes": eps}


def _probe(token: str, h: str, file: str, idx: int) -> bool | None:
    """Real-Debrid has [file] of torrent [h] now: added with only that file, it says "downloaded" within seconds.
    The probe's torrent is deleted again (watch() adds it back, instantly when it's there). None: not known (that
    torrent is being fetched for a watch right now)."""
    with stremio._lock:
        busy = stremio._torrents.setdefault(h, threading.Lock())
    if not busy.acquire(blocking=False):
        return None
    try:
        tid = realdebrid._call(token, "POST", "/torrents/addMagnet", {"magnet": f"magnet:?xt=urn:btih:{h}"})["id"]
        try:
            for _ in range(8):
                info = realdebrid._call(token, "GET", f"/torrents/info/{tid}")
                if info["status"] == "downloaded":
                    return True
                if info["status"] in stremio.DEAD or info["status"] in ("downloading", "compressing", "uploading"):
                    return False
                if info["status"] == "waiting_files_selection":
                    if not (f := stremio._file_of(info, file, idx)):
                        return False
                    realdebrid._call(token, "POST", f"/torrents/selectFiles/{tid}", {"files": str(f["id"])})
                    continue
                time.sleep(1)  # magnet_conversion / queued
            return False
        finally:
            realdebrid._call(token, "DELETE", f"/torrents/delete/{tid}")
    finally:
        busy.release()


def _on_rd(token: str, files: list[dict]) -> dict[str, bool]:
    """{"<hash>|<file>": on Real-Debrid now} for [files]: a day's earlier answers, the first PROBES others probed
    (4 at a time). Torrentio's RD+ flag said "cached" for a file that took 8 minutes (2026-11-15)."""
    if not token:
        return {}
    known = {k: v for k, (t, v) in _read(PROBED).items() if time.time() - t < 86400}
    todo = list({f"{c['hash']}|{c['file']}": c for c in files if f"{c['hash']}|{c['file']}" not in known}.items())[:PROBES]

    def one(item):
        k, c = item
        try:
            return k, _probe(token, c["hash"], c["file"], c["idx"])
        except (OSError, RuntimeError, KeyError, ValueError, TypeError) as e:
            print(f"rd probe {c['file']}: {e}", flush=True)
            return k, None
    with ThreadPoolExecutor(4) as pool:
        got = {k: v for k, v in pool.map(one, todo) if v is not None}
    if got:
        with _lock:
            PROBED.write_text(json.dumps({**_read(PROBED), **{k: [time.time(), v] for k, v in got.items()}}), encoding="utf-8")
    return {**known, **got}


def streams(type_: str, sid: str, found: list[dict] | None = None) -> list[dict]:
    """The best stremio.OPTIONS files per quality, the ones Real-Debrid has (they start at once) first."""
    minutes = stremio._minutes(type_, sid)
    token = os.environ.get("RD_API_TOKEN", "")
    found = stremio.candidates(type_, sid, token) if found is None else found
    picked = []
    for q in stremio.QUALITIES:
        best = {}
        # Streamed to a phone: up to 20 Mbps first (a Remux's 45 stutters over mobile data), then the highest bitrate;
        # AV1 last (the TV can't play it). Torrentio's RD+ only decides which ones get checked first.
        for c in sorted([c for c in found if q in stremio.qualities(c)], key=lambda c: (
                c["cached"] is not True, stremio._mbps(c, minutes) > 20, "AV1" in stremio.tags(c), -stremio._mbps(c, minutes))):
            best.setdefault(c["file"], c)
        picked += [(q, c) for c in list(best.values())[:stremio.OPTIONS]]
    # Probed in turns: each quality's first, then each one's second...
    turns = sorted(range(len(picked)), key=lambda i: [q for q, _ in picked[:i]].count(picked[i][0]))
    on = _on_rd(token, [picked[i][1] for i in turns])
    out = []
    for q, c in picked:
        gb, now = c["gb"], on.get(f"{c['hash']}|{c['file']}")
        size = f"{gb:.1f} GB" if gb >= 1 else f"{gb * 1000:.0f} MB"
        note = "" if now else " · ⏳ Real-Debrid downloads it first" if now is False else " · ⏳ maybe not on Real-Debrid"
        out.append({"q": q, "label": ("▶ starts now · " if now else "") + f"{q} · {size} · {stremio._mbps(c, minutes):.1f} Mbps{note}",
                    "about": stremio.about(c).strip(), "hash": c["hash"], "file": c["file"], "idx": c["idx"], "now": bool(now)})
    out.sort(key=lambda s: not s["now"])  # stable: in quality order within each
    return out


def _direct(token: str, h: str, file: str, idx: int, step) -> str:
    """The file's playable https link: Real-Debrid's link for it (stremio._rd_link) unrestricted."""
    return realdebrid._call(token, "POST", "/unrestrict/link", {"link": stremio._rd_link(token, h, file, idx, step)})["download"]


def _tracks(url: str) -> list[dict]:
    out = jobs._out([jobs.FFPROBE, "-v", "error", "-show_entries", "stream=index,codec_type,codec_name:stream_tags=language,title",
                     "-of", "json", url])
    return json.loads(out).get("streams", [])


def _audio(tracks: list[dict]) -> str:
    """ffmpeg's -map for the Japanese audio: by its language tag, else its name ("Jap": a dual-audio file with no tags
    and the English dub first, Dandadan 2 [RUBaDUB]), else the first audio track."""
    audio = [t for t in tracks if t.get("codec_type") == "audio"]
    tags = lambda t: t.get("tags") or {}  # noqa: E731
    ja = next((t for t in audio if tags(t).get("language", "").lower() in LANGS["ja"]), None) or         next((t for t in audio if re.search(r"(^|[^a-z])(ja|jp|jpn|jap|japanese)|日本", tags(t).get("title", "").lower())), None)
    return f"0:{ja['index']}" if ja else "0:a:0"


def _sub(tracks: list[dict], lang: str) -> dict | None:
    return next((t for t in tracks if t.get("codec_type") == "subtitle" and t.get("codec_name") in TEXT_SUBS
                 and (t.get("tags") or {}).get("language", "").lower() in LANGS[lang]), None)


def _same_release(video: str, sub: str) -> bool:
    """A Jimaku file [sub] timed for the video file [video]: the same CRC or release group, or both Crunchyroll's
    WEB release ([SubsPlease], [Erai-raws], [CR]: the SubsPlease file fitted to a [CR] video moved 0.00 s)."""
    v, s = os.path.basename(video).lower(), os.path.basename(sub).lower()
    crc, gv, gs = re.search(r"[\[(]([0-9a-f]{8})[\])]", v), re.match(r"\[([^\]]+)\]", v), re.match(r"\[([^\]]+)\]", s)
    return bool(crc and crc[1] in s or gv and gs and gv[1] == gs[1] or WEB.search(v) and WEB.search(s))


def _timed(key: str, show: str, season, number, file: str, tmp: Path) -> str:
    """The SRT text of a Jimaku file made for this release (no file read, no fitting), "" if Jimaku has none."""
    found = json.loads(jobs.jimaku.candidates(key, show, season, number, file))
    for i, f in enumerate(found["files"][:4]):
        if _same_release(file, f["name"]):
            try:
                raw = jobs.jimaku.download(key, f["url"], f["name"], number, str(tmp / f"timed{i}"))
            except Exception as e:  # noqa: BLE001 - a zip without this episode: the next one
                print(f"jimaku {f['name']}: {e}", flush=True)
                continue
            jobs._srt(json.loads(jobs.jimaku.cues(raw, "ja")), tmp / "timed.srt")
            print(f"jimaku {f['name']}: timed for {file}, used as it is", flush=True)
            return (tmp / "timed.srt").read_text(encoding="utf-8")
    return ""


def subtitles(url: str, show: str, season, number, file: str, tmp: Path, step, quick: bool = False) -> tuple[str, str, bool]:
    """(Japanese subtitle file text, English or "", timed by name only) for the video at [url]: its own Japanese
    track, else ([quick]) a Jimaku file made for this release, else Jimaku's fitted to its English track or its audio.
    One read of the whole file (ffmpeg), none for a quick Jimaku file."""
    step("reading the file's tracks")
    tracks = _tracks(url)
    ja, en = _sub(tracks, "ja"), _sub(tracks, "en")
    key = os.environ.get("JIMAKU_API_KEY", "")
    if quick and not ja and key:
        step("Japanese subtitles from Jimaku")
        if text := _timed(key, show, season, number, file, tmp):
            return text, "", True
    cmd = [jobs.FFMPEG, "-y", "-v", "error", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
           "-i", url]
    got = {}
    for lang, t in (("ja", ja), ("en", en)):
        if t:
            got[lang] = tmp / f"{lang}.{TEXT_SUBS[t['codec_name']]}"
            cmd += ["-map", f"0:{t['index']}", "-c", "copy" if t["codec_name"] in ("ass", "ssa", "subrip") else "srt", got[lang]]
    audio = tmp / "audio.wav"
    if not ja:  # Jimaku's file is fitted to this release: Parakeet needs its audio when there's no English track
        cmd += ["-map", _audio(tracks), "-vn", "-ac", "1", "-ar", "16000", audio]
    step("reading the file once for its subtitles" + ("" if ja else " and audio") + " (a minute or two)")
    jobs._run(cmd)
    english = got["en"].read_text(encoding="utf-8", errors="replace") if "en" in got else ""
    if ja:
        return got["ja"].read_text(encoding="utf-8", errors="replace"), english, False
    if not key:
        raise ValueError("the file has no Japanese subtitles and the PC has no Jimaku key")
    job = {"jimaku": key, "step": ""}
    step("Japanese subtitles from Jimaku")
    cues, _ = jobs._jimaku(job, audio, show, season, number, file, tmp, got.get("en"))
    if not cues:
        raise ValueError("no Japanese subtitles in the file or on Jimaku: pick another file or title")
    jobs._srt(cues, tmp / "ja.srt")
    return (tmp / "ja.srt").read_text(encoding="utf-8"), english, False


def live_subs(type_: str, sid: str, file: str) -> dict:
    """GET /api/discover/subs: a Jimaku file's lines for the app to show live while the episode is made (SubMiner's
    way: no file read). The file made for this release when Jimaku has one, else its best-ranked file (the app
    offers a nudge). {"cues": [[start, end, text]], "name", "timed"}; no cues when Jimaku has nothing."""
    key = os.environ.get("JIMAKU_API_KEY", "")
    if not key or not stremio.ID.match(sid):
        return {"cues": [], "name": "", "timed": False}
    about = stremio.meta(type_, sid)
    season, number = stremio.numbers(sid)
    found = json.loads(jobs.jimaku.candidates(key, about["name"], season, number, file))["files"][:4]
    for f in sorted(found, key=lambda f: not _same_release(file, f["name"])):  # stable: Jimaku's order otherwise
        try:
            with tempfile.TemporaryDirectory(dir=cfg.work) as tmp:
                raw = jobs.jimaku.download(key, f["url"], f["name"], number, tmp)
                cues = json.loads(jobs.jimaku.cues(raw, "ja"))
        except Exception as e:  # noqa: BLE001 - a zip without this episode: the next one
            print(f"jimaku live {f['name']}: {e}", flush=True)
            continue
        if cues:
            return {"cues": cues, "name": f["name"], "timed": _same_release(file, f["name"])}
    return {"cues": [], "name": "", "timed": False}


def _moved(a: str, b: str) -> float:
    """How far subtitle file [b]'s lines sit from [a]'s (median over lines with the same text, s)."""
    first = {t: s for s, _, t in subtitle_cues(a)}
    gaps = sorted(abs(first[t] - s) for s, _, t in subtitle_cues(b) if t in first)
    return gaps[len(gaps) // 2] if gaps else 99.0


def _next_id(type_: str, sid: str) -> str:
    try:
        eps = [e[0] for e in stremio.meta(type_, sid)["episodes"]]
    except (OSError, ValueError):
        return ""
    return eps[eps.index(sid) + 1] if sid in eps and eps.index(sid) + 1 < len(eps) else ""


def _prepare_next(tv, person: str, lang: str, type_: str, nxt: str, h: str, file: str) -> None:
    """The next episode [nxt] got ready while this one plays: its file from torrent [h] (a season pack) if
    Real-Debrid has it, else the best one Real-Debrid has in the same quality; watch.json "ep|…" remembers it for the
    app. A pack's next file isn't always there: Frieren's E3-E5 were, E6 had to download (2026-11-15)."""
    if f"ep|{person}|{lang}|{nxt}" in _done():
        return
    token = os.environ.get("RD_API_TOKEN", "")
    found = stremio.candidates(type_, nxt, token)
    pick = next((c for c in stremio.ranked(found, stremio._minutes(type_, nxt), h) if c["hash"] == h), None)
    if pick and not _on_rd(token, [pick]).get(f"{pick['hash']}|{pick['file']}"):
        pick = None
    if not pick:
        q = next((q for q, pat in stremio.RESOLUTION.items() if re.search(pat, file.lower())), None)
        files = streams(type_, nxt, found)
        pick = next((f for f in files if f["now"] and f["q"] == q), None) or next((f for f in files if f["now"]), None)
    if not pick:
        return  # nothing that starts at once: the app's file list then
    _remember(**{f"ep|{person}|{lang}|{nxt}": {"hash": pick["hash"], "file": pick["file"], "idx": pick["idx"]}})
    watch(tv, {"person": person, "lang": lang, "type": type_, "id": nxt, "hash": pick["hash"], "file": pick["file"],
               "idx": str(pick["idx"]), "prep": "1"})


def watch(tv, body: dict) -> dict:
    lang = str(body.get("lang") or "ja")
    # No profile picked (a one-person TV): the first person learning that language
    person = str(body.get("person") or next((p for p, libs in sync.PEOPLE.items() if lang in libs), ""))
    type_, sid = str(body.get("type", "")), str(body.get("id", ""))
    h, file, idx = str(body.get("hash") or "").lower(), str(body.get("file") or ""), str(body.get("idx") or "-1")
    if not h:  # only the episode: the file got ready for it (_prepare_next), if any
        pick = _done().get(f"ep|{person}|{lang}|{sid}")
        if not pick:
            return {"state": "none"}
        h, file, idx = pick["hash"], pick["file"], str(pick["idx"])
    if lang not in sync.PEOPLE.get(person, {}) or type_ not in ("movie", "series") or not stremio.ID.match(sid) \
            or not stremio.HASH.match(h) or not file or not re.fullmatch(r"-?\d+", idx):
        raise ValueError("bad watch request")
    key = f"{person}|{lang}|{h}|{file}"
    nxt = _next_id(type_, sid)
    with _lock:
        job = _running.get(key)
        if job and job["prep"] and not body.get("prep"):  # got ready ahead, now watched: its next one gets ready too
            job["prep"] = False
            if job["state"] == "ready" and nxt:
                threading.Thread(target=_prepare_next, args=(tv, person, lang, type_, nxt, h, file), daemon=True).start()
        if job and job["state"] in ("ready", "failed"):
            del _running[key]  # reported once; asking again starts over (a fresh link: Real-Debrid's expire)
            return job
        if job:
            return job
        job = _running[key] = {"state": "running", "step": "asking Real-Debrid", "error": "", "next": nxt,
                               "prep": bool(body.get("prep")), "file": file}

    def run():
        token = os.environ.get("RD_API_TOKEN", "")
        quick = ""
        try:
            url = _direct(token, h, file, int(idx), lambda s: job.update(step=s))
            eid = _done().get(key)
            if not (eid and (STREAM / f"{eid}.kumapie.json").exists()):
                job.update(state="video", url=url)  # the app plays it now; the words follow
                about = stremio.meta(type_, sid)
                season, number = stremio.numbers(sid)
                title = str(body.get("title") or "")[:200] or (f"{about['name']} {number}" if number else about["name"])
                made = {"person": person, "lang": lang, "title": title, "show": about["name"]}
                with tempfile.TemporaryDirectory(dir=cfg.work) as tmp:
                    ja, en, timed = subtitles(url, about["name"], season, number, file, Path(tmp),
                                              lambda s: job.update(step=s), quick=True)
                job.update(step="finding the words (Japanese: about 30 s)")
                eid = tv.stream({**made, "subs": ja, "english": en})["id"]
                _remember(**{key: eid})
                quick = ja if timed else ""
            job.update(state="ready", id=eid, url=url, step="")
        except Exception as e:  # noqa: BLE001 - every failure is shown on the phone / TV
            job.update(state="failed", error=str(e)[:300])
            return
        if quick:  # Jimaku's file was taken by its name: fitted now, the episode made again if it moved (or gains English)
            try:
                with tempfile.TemporaryDirectory(dir=cfg.work) as tmp:
                    ja, en, _ = subtitles(url, about["name"], season, number, file, Path(tmp), lambda s: None)
                moved = _moved(quick, ja)
                print(f"discover: {file} timed by name, alass moved it {moved:.2f} s", flush=True)
                if moved > 0.5 or en:  # the same episode id: where it was left stays
                    tv.stream({**made, "id": eid, "subs": ja if moved > 0.5 else quick, "english": en})
            except Exception as e:  # noqa: BLE001 - the quick episode stays
                print(f"discover: fitting {file} failed: {e}", flush=True)
        if job["next"] and not job["prep"]:
            try:
                _prepare_next(tv, person, lang, type_, job["next"], h, file)
            except Exception as e:  # noqa: BLE001 - the app's file list then
                print(f"discover: next episode after {sid}: {e}", flush=True)
    threading.Thread(target=run, daemon=True).start()
    return job


if __name__ == "__main__":
    assert _sub([{"index": 2, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}}], "ja")["index"] == 2
    assert not _sub([{"index": 2, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "jpn"}}], "ja")
    dual = [{"index": 1, "codec_type": "audio", "tags": {"title": "Eng"}}, {"index": 2, "codec_type": "audio", "tags": {"title": "Jap"}}]
    assert _audio(dual) == "0:2" and _audio(dual[:1]) == "0:a:0"
    assert _audio([{"index": 1, "codec_type": "audio", "tags": {"language": "eng"}}, {"index": 3, "codec_type": "audio", "tags": {"language": "jpn"}}]) == "0:3"
    cr = "[CR] Sousou no Frieren - 01 [720p] [90BE6BB0].mkv"
    assert _same_release(cr, "[SubsPlease] Sousou no Frieren - 01 (1080p) [ABCD1234].ja.srt")
    assert _same_release("[Judas] Frieren - 01.mkv", "[Judas] Frieren - 01.ass")
    assert _same_release("Frieren - 01 [90BE6BB0].mkv", "Frieren 01 [90be6bb0].srt")
    assert not _same_release(cr, "[Netflix] Sousou no Frieren - 01.srt")
    assert not _same_release("[Judas] Frieren - 01.mkv", "Frieren.S01E01.WEBRip.Netflix.ja.srt")
    a = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n2\n00:00:05,000 --> 00:00:06,000\nさようなら\n"
    assert _moved(a, a) == 0 and abs(_moved(a, a.replace("00:00:0", "00:00:1")) - 10) < 0.01
    print("ok")
