#!/usr/bin/env python3
"""German feed for Pedro's phone: a vertical swipe page of short i+0 / i+1 clips (port 8769).

Reads state\\feed\\feed.db (written by tools\\feed\\feed.py build/gloss) and the known words kumapie on the
phone sends (known.py: state\\feed\\known\\<person>.json), plus the words marked known on the TV.

  /                       the page (static/), installable to the home screen
  POST /api/feed          {"n": 10, "exclude": [ids shown]} -> random i+0 / i+1 clips with lines and
                          dictionary meanings
  /clips/<id>.mp4         the clip's episode video, with HTTP Range (the page plays start..end of it)
  /api/tts?w=<word>       the word read by edge-tts (only words in the feed), cached in state\\feed\\tts\\
  POST /api/tv/known_words  {"person", "lang", "words"}: the words kumapie shows as known (known.py)
  POST /api/tv/device_seen  {"person", "seen", "unwatched"}: what the person's phones count as seen (tv.py)
  POST /api/tv/device_watch {"person", "watch": {lang: {day: seconds}}}: the phones' own time per language (tv.py)
  GET  /api/models/<file>   the Japanese splitter's data files (MODEL_FILES, from state\\dicts)
  POST /api/tv/meta         {"show", "kind", "source", "id"}: "Fix match" for a PC show (tv.py, meta.py)
  /api/tv/...             the kumapie-tv app's API (Android TV, whole episodes scene by scene): see tv.py
  /api/sync/...           episodes made on phones/tablets, kept here for the person's other devices: see sync.py

Binds 127.0.0.1: Tailscale Serve publishes it on HTTPS 8445 (tailnet only, never Funnel).
"""
import asyncio
import hashlib
import shutil
import json
import mimetypes
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # code/
from dojo import cfg  # noqa: E402
from dojo.selfrestart import restart_on_change  # noqa: E402
from tv import TV  # noqa: E402
import dicts  # noqa: E402
import sync  # noqa: E402
import known  # noqa: E402
import jobs  # noqa: E402

F = cfg.raw["feed"]
PORT = cfg.services["feed_port"]
STATE = cfg.state / "feed"
DB = STATE / "feed.db"
TTS = STATE / "tts"
STATIC = Path(__file__).parent / "static"
CLIP_ID = re.compile(r"^/clips/([0-9a-f]{12})\.mp4$")

lock = threading.Lock()
feed = {"clips": {}, "words": set(), "status": frozenset(), "levels": {}, "db_mtime": None}
tts_lock = threading.Lock()
tv = None  # TV(...) once segments() is defined


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def mmss(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def segments(text, tokens):
    """Cue text -> [[text, inflection or None], ...] covering all of it, words split out."""
    out, pos = [], 0
    for surface, at, infl, _ in sorted(tokens, key=lambda t: t[1]):
        if not infl or at < pos:
            continue
        if at > pos:
            out.append([text[pos:at], None])
        out.append([surface, infl])
        pos = at + len(surface)
    if pos < len(text):
        out.append([text[pos:], None])
    return out


def mtime(path):
    """Newest mtime of a SQLite file and its -wal (writes land in the -wal first)."""
    times = [p.stat().st_mtime for p in (path, path.with_name(path.name + "-wal")) if p.exists()]
    return max(times) if times else None


def load_db():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        clips = {}
        for cid, title, source, start, end, english in db.execute(
                "select id, title, source, start, end, english from clips where built = 1"):
            clips[cid] = {"id": cid, "title": title, "at": mmss(start), "source": source, "start": start, "end": end,
                          "english": english, "cues": [], "infl": set()}
        words = set()
        for cid, s, e, text, toks in db.execute(
                "select clip_id, start, end, text, tokens from cues order by clip_id, idx"):
            c = clips.get(cid)
            if not c:
                continue
            toks = json.loads(toks)
            c["cues"].append({"start": s, "end": e, "seg": segments(text, toks)})
            for surface, _, infl, _ in toks:
                if infl:
                    c["infl"].add(infl)
                    words.add(surface.lower())
        tv.load(db)
        return clips, words
    finally:
        db.close()


def relevel():
    """Clip level = distinct inflections not known."""
    st = feed["status"]
    feed["levels"] = {cid: sum(1 for i in c["infl"] if i not in st) for cid, c in feed["clips"].items()}
    pool = sum(1 for v in feed["levels"].values() if v <= 1)
    log(f"{len(feed['clips'])} clips, {pool} in the feed (i+0/i+1), {len(st)} known words")


def reload():
    """Reread feed.db and the known words when they changed."""
    changed = False
    m = mtime(DB)
    if m and m != feed["db_mtime"]:
        try:
            clips, words = load_db()
            with lock:
                feed.update(clips=clips, words=words, db_mtime=m)
            changed = True
        except sqlite3.Error as e:
            log(f"could not read {DB}: {e}")
    status = status_of(F["person"])
    if status != feed["status"]:
        with lock:
            feed["status"] = status
        changed = True
    if changed:
        with lock:
            relevel()


def status_of(key):
    """A person's known words: what their phone sent, plus (the feed person) the words marked known on the TV.
    None when their phone never sent any (the TV then shows their episodes plain)."""
    ws = known.words(key)
    if key != F["person"]:
        return ws
    return frozenset(ws or ()) | tv.known_words()


def background():
    while True:
        reload()
        try:
            tv.synced(force=True)  # new or changed .kumapie.json files, here so no request waits on the scan
        except Exception as e:
            log(f"library scan failed: {e}")
        time.sleep(60)


def mark_known(word, on):
    """The TV's "mark known" (or undo): the word file, then the feed's levels at once."""
    tv.mark_known(word, on)
    reload()
    return {"ok": True, "s": "k" if word in feed["status"] else "u"}


def pick(n, exclude):
    """n random clips: i1_share of them i+1, the rest i+0 (each side tops up the other).
    wrapped = everything had been shown, so the page starts over."""
    with lock:
        levels = feed["levels"]
        pool = [cid for cid, lv in levels.items() if lv <= 1]
        fresh = [cid for cid in pool if cid not in exclude]
        i1 = [c for c in fresh if levels[c] == 1]
        i0 = [c for c in fresh if levels[c] == 0]
        random.shuffle(i1)
        random.shuffle(i0)
        k1 = min(len(i1), max(n - len(i0), round(n * F.get("i1_share", 0.5))))
        chosen = i1[:k1] + i0[:n - k1]
        random.shuffle(chosen)
        # Out of unseen clips: the last ones first, then start over with the rest.
        wrapped = len(chosen) < n and len(pool) > len(chosen)
        if wrapped:
            rest = [c for c in pool if c not in chosen]
            random.shuffle(rest)
            chosen += rest[:n - len(chosen)]
        st = feed["status"]
        out = []
        for cid in chosen:
            c = feed["clips"][cid]
            # Meanings from the dictionary only (user, 2026-10-15), as on the TV.
            words = {i: {"s": "k" if i in st else "u", "g": dicts.look("de-en", tv.lemmas.get(i, ""), i)}
                     for i in c["infl"]}
            out.append({"id": cid, "start": c["start"], "end": c["end"], "title": c["title"], "at": c["at"], "english": c["english"],
                        "level": levels[cid], "cues": c["cues"], "words": words})
        return {"clips": out, "wrapped": wrapped,
                "pool": {"i0": sum(1 for v in levels.values() if v == 0),
                         "i1": sum(1 for v in levels.values() if v == 1)}}


VOICES = {"de": F.get("tts_voice", "de-DE-KatjaNeural"), "en": "en-US-AvaNeural", "ja": "ja-JP-NanamiNeural"}


# Jiten's word database (tools\kumapie\jiten_lexicon.py) and Jiten's Sudachi user dictionary, rebuilt for Sudachi
# Java 0.8: the phones download them for kumapie's Japanese splitter (kumapie_plan.md item 26b).
MODEL_FILES = {"jiten-lexicon.tsv.gz", "jiten_user_core.dic"}


def tts(word, lang="de"):
    """Word audio from edge-tts in the episode's language, made once per word (German files keep their old names)."""
    key = word if lang == "de" else f"{lang}|{word}"
    out = TTS / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".mp3")
    if out.exists():
        return out
    import edge_tts

    async def run(tmp):
        await edge_tts.Communicate(word, VOICES.get(lang, VOICES["de"])).save(str(tmp))

    TTS.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with tts_lock:
        if not out.exists():
            for attempt in range(3):  # the service drops connections now and then
                try:
                    asyncio.run(run(tmp))
                    if tmp.stat().st_size:
                        tmp.replace(out)
                        break
                except Exception as e:
                    log(f"edge-tts failed for {word!r}: {e}")
                    time.sleep(1)
    return out if out.exists() else None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        if path.startswith("/api/tv"):
            return self.tv_get(path, urllib.parse.parse_qs(url.query).get("person", [None])[0])
        if path.startswith("/api/sync/"):
            return self.sync_get(path.split("/")[3:])
        if path == "/api/aniworld":
            return self.send_json(jobs.listing())
        if path.startswith("/api/models/"):  # files the phones' Japanese splitter needs (item 26b), from state\dicts
            name = path.rsplit("/", 1)[-1]
            f = cfg.state / "dicts" / name
            if name in MODEL_FILES and f.is_file():
                return self.send_file(f, "application/octet-stream", cache=True)
            return self.send_error(404)
        m = CLIP_ID.match(path)
        if m:
            with lock:
                c = feed["clips"].get(m.group(1))
            f = cfg.library(F["person"]) / c["source"] if c else None
            if f and f.is_file():  # the whole episode: the page seeks to the clip's start and stops at its end
                return self.send_file(f, "video/mp4", cache=False)
            return self.send_error(404)
        if path == "/api/tts":
            query = urllib.parse.parse_qs(url.query)
            word = query.get("w", [""])[0].strip().lower()
            with lock:
                ok = word in feed["words"] or word in tv.words
            f = tts(word, query.get("lang", ["de"])[0]) if ok else None
            return self.send_file(f, "audio/mpeg", cache=True) if f else self.send_error(404)
        name = "index.html" if path == "/" else path.lstrip("/")
        f = STATIC / name
        if "/" not in name and "\\" not in name and f.is_file():
            ctype = {".webmanifest": "application/manifest+json", ".js": "text/javascript"}.get(
                f.suffix, mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            if ctype.startswith("text/") or ctype.endswith("json"):
                ctype += "; charset=utf-8"
            return self.send_file(f, ctype, cache=False)
        self.send_error(404)

    def tv_get(self, path, person=None):
        status = status_of
        if path == "/api/tv":
            return self.send_json(tv.info())
        if path == "/api/tv/shows":
            return self.send_json(tv.shows(status, person))
        if path == "/api/tv/stats":
            return self.send_json(tv.stats(person))
        if path == "/api/tv/people":
            return self.send_json(tv.profiles())
        if path == "/api/tv/history":
            return self.send_json(tv.history(person))
        route = {name: r.match(path) for name, r in tv.routes.items()}
        if route["episode"]:
            data = tv.episode(route["episode"].group(1), status)
            return self.send_json(data) if data else self.send_error(404)
        if route["video"]:
            f = tv.video(route["video"].group(1))
            return self.send_file(f, "video/mp4", cache=False) if f else self.send_error(404)
        if route["file"]:
            f = tv.file(route["file"].group(1), route["file"].group(2))
            ctype = {".webp": "image/webp", ".png": "image/png"}.get(f.suffix, "audio/ogg") if f else ""
            return self.send_file(f, ctype, cache=True) if f else self.send_error(404)
        if route["thumb"] or route["poster"]:
            f = tv.thumb(route["thumb"].group(1)) if route["thumb"] else tv.poster(route["poster"].group(1))
            return self.send_file(f, "image/jpeg", cache=False) if f else self.send_error(404)
        self.send_error(404)

    def sync_get(self, parts):
        if len(parts) == 1 and parts[0] in sync.PEOPLE:
            return self.send_json(sync.listing(parts[0]))
        if len(parts) == 2 and parts[1] == "deleted" and parts[0] in sync.PEOPLE:
            return self.send_json(sync.deleted(parts[0]))
        if len(parts) == 3 and parts[2] == "status" and sync.valid(parts[0], parts[1]):
            return self.send_json(sync.status(parts[0], parts[1]))
        if len(parts) == 3 and sync.valid(*parts):
            f = sync.stored(*parts)
            ctype = {"video.mp4": "video/mp4", "thumb.jpg": "image/jpeg"}.get(parts[2], "application/json; charset=utf-8")
            return self.send_file(f, ctype, cache=False) if f else self.send_error(404)
        self.send_error(404)

    def do_PUT(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path.startswith("/api/aniworld/upload/"):  # a video for a job on the PC (services\feed\jobs.py)
            parts = [urllib.parse.unquote(p) for p in url.path.split("/")[4:]]
            q = urllib.parse.parse_qs(url.query)
            try:
                if len(parts) != 2:
                    raise ValueError(0)
                size = jobs.receive(parts[0], parts[1], int(q.get("offset", ["0"])[0]), int(q.get("total", ["0"])[0]),
                                    self.rfile.read, int(self.headers.get("Content-Length") or 0))
            except ValueError as e:
                data = json.dumps({"size": e.args[0] if e.args and isinstance(e.args[0], int) else 0}).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            return self.send_json({"size": size})
        parts = url.path.split("/")[3:] if url.path.startswith("/api/sync/") else []
        if len(parts) != 3 or not sync.valid(*parts):
            return self.send_error(404)
        try:
            offset = int(urllib.parse.parse_qs(url.query).get("offset", ["0"])[0])
            size = sync.receive(*parts, offset, self.rfile.read, int(self.headers.get("Content-Length") or 0))
        except ValueError as e:  # not where the PC is: the device starts again from the size it has
            data = json.dumps({"size": e.args[0] if e.args and isinstance(e.args[0], int) else 0}).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        self.send_json({"size": size})

    def do_POST(self):
        path = self.path.split("?")[0]
        parts = path.split("/")[3:] if path.startswith("/api/sync/") else []
        if len(parts) == 3 and parts[2] == "done" and sync.valid(parts[0], parts[1]):
            try:
                meta = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                done = sync.finish(parts[0], parts[1], meta)
            except ValueError as e:
                return self.send_error(400, str(e))
            log(f"sync: {parts[0]} uploaded {done['show']} / {done['title']}")
            return self.send_json(done)
        if len(parts) == 3 and parts[2] == "delete" and sync.valid(parts[0], parts[1]):
            done = sync.delete(parts[0], parts[1])
            log(f"sync: {parts[0]} deleted everywhere {done['show']} / {done['title']} ({parts[1]})")
            return self.send_json(done)
        if len(parts) == 3 and parts[2] == "episode" and sync.valid(parts[0], parts[1]):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                done = sync.retime(parts[0], parts[1], body.get("episode") or {}, body.get("shifted") or 0)
            except (ValueError, TypeError) as e:
                return self.send_error(400, str(e))
            log(f"sync: {parts[0]} re-timed {done['show']} / {done['title']} ({done['shifted']:+.2f} s)")
            return self.send_json(done)
        if path == "/api/aniworld":
            try:
                job = jobs.submit(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
            except ValueError as e:
                return self.send_error(400, str(e))
            log(f"jobs: {job['person']} queued {job['source']} {job['url']} ({job['lang']})")
            return self.send_json(job)
        if path not in ("/api/feed", "/api/tv/progress", "/api/tv/lookup", "/api/tv/known", "/api/tv/known_words",
                        "/api/tv/device_seen", "/api/tv/device_watch", "/api/tv/meta"):
            return self.send_error(404)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if path == "/api/tv/progress":
                return self.send_json(tv.progress(body))
            if path == "/api/tv/lookup":
                return self.send_json(tv.lookup(body))
            if path == "/api/tv/device_seen":
                return self.send_json(tv.device_seen(body))
            if path == "/api/tv/device_watch":
                return self.send_json(tv.device_watch(body))
            if path == "/api/tv/meta":
                return self.send_json(tv.set_meta(body))
            if path == "/api/tv/known":
                return self.send_json(mark_known(str(body.get("word", "")), bool(body.get("known"))))
            if path == "/api/tv/known_words":
                done = known.save(body)
                log(f"known words: {done['key']} {done['n']}")
                reload()
                return self.send_json(done)
            n = max(1, min(int(body.get("n", 10)), 50))
            exclude = set(body.get("exclude", []))
        except (ValueError, TypeError, AttributeError):
            return self.send_error(400)
        self.send_json(pick(n, exclude))

    def send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, f, ctype, cache):
        size = f.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(size - int(m.group(2)), 0)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        # Clip ids change when a clip changes, so its files never do; the page itself is never cached.
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if cache else "no-cache")
        self.end_headers()
        try:
            with f.open("rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(left, 256 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass  # the phone moved on (scrolled away, or seeks with a new range)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    STATE.mkdir(parents=True, exist_ok=True)
    tv = TV(segments, os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg")
    reload()
    threading.Thread(target=background, daemon=True).start()
    log(f"dojo-feed on http://127.0.0.1:{PORT}")
    restart_on_change(Path(__file__).parent, log=log)  # new code -> exit, NSSM starts it again (no admin needed)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
