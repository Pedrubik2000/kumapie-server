"""API for the kumapie-tv app (Android TV, plan: code\\kumapie_tv_plan.md): whole episodes, scene by scene.

Reads the scenes that `tools\\feed\\feed.py scenes` writes to feed.db, plus every episode with a
<stem>.kumapie.json (same JSON as /api/tv/episode) in the libraries of the people with kumapie ([sync.people]): made
on a phone or tablet and synced (sync.py, ids local-<hex>) or made on the PC from the library's subtitles
(tools\\kumapie\\pc_episodes.py). `?person=<sync person>` picks whose shows and history (default: the feed person,
the TV). Keeps the TV's progress in
state\\feed\\tv.db (resume point per episode, scenes seen, seconds watched per day). Served by server.py:

  GET  /api/tv                       {"api": 1, language, translation}: the app's server check
  GET  /api/tv/shows                 shows with their episodes, scenes seen and resume points
  GET  /api/tv/episode/<id>          the episode's scenes: times, German lines split into words, English,
                                     level; "words" {inflection: {"s": k/l/u, "g": meaning}}, a scene's own
                                     meaning in its "g" when the word means something else there
                                     A scene's "defs" {"<line>|<word>": {"lemma", "en", "de": segments, "gr"}}
                                     are its per-line definitions (definitions v2); words inside "de" are in
                                     "words" too, so they can be picked
  GET  /api/tv/video/<id>            the episode's .mp4 from library\\ (HTTP Range; the app seeks to scenes)
  GET  /api/tv/file/<id>/img/<name>.webp a novel's screens (and mask/<name>.png wipes): a scene's "base" and "pics"
  GET  /api/tv/file/<id>/bgm/<name>.ogg  a novel's music (tools\\vn\\higurashi.py): a scene's "bgm", played
                                     under it while it waits; "novel": true episodes pause after every scene (line)
  GET  /api/tv/thumb/<id>.jpg        a frame of the episode (made once, cached in state\\feed\\tv\\)
  GET  /api/tv/poster/<show>.jpg     the show's cover (cover.jpg/poster.jpg); 404 without one (the apps draw a neutral poster)
  POST /api/tv/progress              {"episode", "pos", "seen": [scene ids], "watched": seconds}
  POST /api/tv/lookup                {"word": inflection, "scene": id}: a word looked up in the picker
  POST /api/tv/known                 {"word": inflection, "known": true|false}: mark a word known without a
                                     card (state\\morphs\\<person>-known-words.txt, which morphs recalc reads
                                     too) or undo it; answers the word's new status
  GET  /api/tv/stats                 {"days": {date: seconds}, "today", "week", "total" (seconds), "streak" (days),
                                     "scenes_seen", "scenes_total", "lookups", "words_looked_up",
                                     "top_lookups": [[word, times, meaning]], "marked_known",
                                     "shows": [{"title", "seen", "scenes"}]}  (days start at 4 am)
  GET  /api/tv/people                the TV's "Who's watching?": [{"id": sync person, "name", "menu": "en"/"es"}]
  GET  /api/tv/history               everything recorded, for kumapie's progress note in Anki: {"pos": {episode:
                                     [pos, epoch s]}, "seen": {scene: [episode, first, last, times]}, "watch": {day:
                                     seconds}, "lwatch": {lang: {day: seconds}}, "lookups": {word: times}, "marked":
                                     {word: [true, 0]}, "gloss": {word: meaning}}
  POST /api/tv/device_watch          {"person", "watch": {lang: {day: seconds}}}: the person's phones' own time
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from dojo import cfg
import dicts
import known
import sync
import sys
sys.path.insert(0, str(cfg.kumapie_app / "mobile" / "src" / "main" / "python"))
import meta as showmeta  # noqa: E402  the phone's metadata module (AniList/Jikan/Kitsu, TVmaze, Wikidata), item 23

API = 1
F = cfg.raw["feed"]
PERSON = cfg.person(F["person"])
STATE = cfg.state / "feed"
TV_DB = STATE / "tv.db"
KNOWN_WORDS = cfg.state / "morphs" / f"{F['person']}-known-words.txt"  # marked known on the TV (morphs reads it)
CACHE = STATE / "tv"
# What each person's phones count as seen (all their devices merged, the TV's scenes included) and their "Mark as not
# watched" times: POST /api/tv/device_seen after every progress sync on a phone (kumapie_plan.md item 14).
DEVICE_SEEN = STATE / "device_seen.json"
# Each person's phones' watch time per language: {person: {lang: {day: seconds}}}, without the TV's (POST /api/tv/device_watch).
DEVICE_WATCH = STATE / "device_watch.json"
# Show metadata (items 20-23): META/<show folder>.json (meta.py's details, or {"none": true} when nothing matched) and
# .jpg (its poster). Looked up once per show in the background; "Fix match" on a phone replaces it (POST /api/tv/meta).
META = STATE / "meta"
# Home category -> meta.py kind; YouTube channels and reels: the channel's picture (found from one of its video links)
META_KINDS = {"anime": "anime", "shows": "shows", "movies": "movies", "visual_novels": "visual_novels", "youtube": "youtube", "reels": "youtube"}
ID = r"((?:local-)?[0-9a-f]{12})"
LANG = {key: lang for libs in sync.PEOPLE.values() for lang, key in libs.items()}  # library -> its language
ROUTES = {
    "episode": re.compile(rf"^/api/tv/episode/{ID}$"),
    "video": re.compile(rf"^/api/tv/video/{ID}$"),
    "thumb": re.compile(rf"^/api/tv/thumb/{ID}\.jpg$"),
    "poster": re.compile(r"^/api/tv/poster/([a-z0-9_]+)\.jpg$"),
    "file": re.compile(rf"^/api/tv/file/{ID}/(bgm/[\w.-]+\.ogg|img/\w+\.webp|mask/[\w.-]+\.png)$"),
}
KINDS = ["anime", "shows", "movies", "youtube", "visual_novels", "reels"]  # order of the rows on Home (reels: feed only)

TV_SCHEMA = """
create table if not exists progress (episode text primary key, pos real, updated text);
create table if not exists seen (scene text primary key, episode text, first text, last text, times integer);
create table if not exists watch (day text primary key, seconds real);
create table if not exists lookups (word text, scene text, at text);
create table if not exists watch_p (day text, person text, seconds real, primary key (day, person));
"""


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def episode_id(source: str) -> str:
    return hashlib.sha1(source.encode()).hexdigest()[:12]


def study_day() -> str:
    """The day as Anki counts it: a new day starts at 4 am."""
    return (datetime.datetime.now() - datetime.timedelta(hours=4)).date().isoformat()


class TV:
    def __init__(self, segments, ffmpeg: str):
        self.segments = segments  # server.segments: cue text + tokens -> [[text, inflection or None]]
        self.ffmpeg = ffmpeg
        self.routes = ROUTES
        self.episodes: dict[str, dict] = {}  # id -> episode with its scenes
        self.words: set[str] = set()  # surface forms, lower case (for /api/tts)
        self.write_lock = threading.Lock()
        self.lemmas: dict[str, str] = {}
        self.thumb_lock = threading.Lock()
        self.synced_lock = threading.Lock()
        self.scanned = None  # (sync.json's mtime, time) of the last look at the libraries
        self.parsed: dict[Path, tuple] = {}  # .kumapie.json -> ((mtime, size), its JSON): unchanged files aren't read again
        STATE.mkdir(parents=True, exist_ok=True)
        with self.tv_db() as db:
            db.executescript(TV_SCHEMA)
            # Per person since "Who's watching?" (2026-10-07): what was recorded before is the feed person's.
            if "person" not in {r[1] for r in db.execute("pragma table_info(lookups)")}:
                db.execute("alter table lookups add column person text")
                db.execute("update lookups set person = ?", (F["person"],))
            if not db.execute("select 1 from watch_p limit 1").fetchone():
                db.execute("insert into watch_p select day, ?, seconds from watch", (F["person"],))

    @contextlib.contextmanager
    def tv_db(self):
        """tv.db, committed and closed at the end of the block."""
        db = sqlite3.connect(TV_DB, timeout=30)
        try:
            db.execute("pragma journal_mode = wal")
            with db:
                yield db
        finally:
            db.close()

    # ------------------------------------------------------------ loading feed.db

    def load(self, db: sqlite3.Connection) -> None:
        """Read episodes, scenes, cues and meanings (feed.db opened read-only by the server)."""
        tables = {r[0] for r in db.execute("select name from sqlite_master where type = 'table'")}
        if "scenes" not in tables:
            return
        # Meanings come from the dictionaries only (user, 2026-10-15): feed.db's AI glosses and line definitions
        # (feed.py gloss / defs) aren't served any more.
        episodes = {}
        for source, kind, show, show_title, title, season, number, duration in db.execute(
                "select source, kind, show, show_title, title, season, episode, duration from episodes"):
            eid = episode_id(source)
            episodes[eid] = {"id": eid, "person": F["person"], "pair": "de-en", "source": source, "kind": kind, "show": show, "show_title": show_title,
                             "title": title or show_title, "season": season, "episode": number,
                             "duration": duration, "scenes": [], "infl": set()}
        scenes = {}
        for sid, source, idx, start, end, german, english in db.execute(
                "select id, source, idx, start, end, german, english from scenes order by source, idx"):
            ep = episodes.get(episode_id(source))
            if ep:
                sc = {"id": sid, "i": idx, "start": start, "end": end, "german": bool(german),
                      "english": json.loads(english), "cues": [], "infl": set()}
                ep["scenes"].append(sc)
                scenes[sid] = (ep, sc)
        words = set()
        lemmas: dict[str, str] = {}
        for sid, start, end, text, toks in db.execute(
                "select scene_id, start, end, text, tokens from scene_cues order by scene_id, idx"):
            if sid not in scenes:
                continue
            ep, sc = scenes[sid]
            toks = json.loads(toks)
            sc["cues"].append({"start": start, "end": end, "text": text, "seg": self.segments(text, toks)})
            for surface, _, infl, lemma in toks:
                if infl:
                    sc["infl"].add(infl)
                    ep["infl"].add(infl)
                    words.add(surface.lower())
                    if lemma:
                        lemmas.setdefault(infl, lemma)
        for ep in episodes.values():
            ep["gloss"] = {i: dicts.look("de-en", lemmas.get(i, ""), i) for i in ep["infl"]}
        self.episodes, self.words, self.lemmas = episodes, words, lemmas
        with self.synced_lock:
            self.scanned = None  # the libraries' episodes join again on the next request

    def synced(self, force: bool = False) -> None:
        """Adds the libraries' episodes with a .kumapie.json. A request looks again only after a sync upload or a
        feed.db reload; server.background() looks every minute (force), so opening kumapie never waits on that."""
        try:
            mtime = sync.INDEX.stat().st_mtime
        except OSError:
            mtime = None
        if not force and self.scanned and self.scanned[0] == mtime:
            return
        with self.synced_lock:
            if not force and self.scanned and self.scanned[0] == mtime:
                return  # another thread just looked
            parsed = {}
            listed = {e["id"]: e for person in sync._index().values() for e in person.values()}
            episodes = {k: v for k, v in self.episodes.items() if "path" not in v}  # feed.db's
            words, lemmas = set(self.words), dict(self.lemmas)
            for key in LANG:
                root = cfg.library(key)
                for f in sorted(root.rglob("*.kumapie.json")) if root.is_dir() else []:
                    try:
                        st = f.stat()
                        stamp = (st.st_mtime_ns, st.st_size)
                        old = self.parsed.get(f)
                        data = old[1] if old and old[0] == stamp else json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    parsed[f] = (stamp, data)
                    video = f.with_name(f.name.removesuffix(".kumapie.json") + ".mp4")
                    if not video.is_file() or "id" not in data:
                        continue
                    e = listed.get(data["id"], data)  # a synced one's listing has its kind, season and number
                    ep = {"id": data["id"], "person": key, "source": e.get("source", ""), "path": video,
                          "kind": "reels" if e.get("kind") == "reels" else sync.KIND_DIR.get(e.get("kind"), f.relative_to(root).parts[0]), "show": f.parent.name,
                          "show_title": e.get("show") or data.get("show", ""), "title": e.get("title") or data.get("title", ""),
                          "season": e.get("season"), "episode": e.get("number"),
                          "duration": e.get("duration") or data.get("duration"), "scenes": [], "infl": set(),
                          "novel": data.get("novel", False), "match": e.get("match")}
                    for sc in data.get("scenes", []):
                        segs = [seg for c in sc.get("cues", []) for seg in c.get("seg", []) if seg[1]]
                        infl = {i for _, i in segs}
                        words |= {t.lower() for t, _ in segs}
                        ep["scenes"].append({"id": sc["id"], "i": sc["i"], "start": sc["start"], "end": sc["end"],
                                             "german": sc.get("german", True), "english": sc.get("english", []),
                                             "cues": sc.get("cues", []), "infl": infl, "bgm": sc.get("bgm"),
                                             "base": sc.get("base"), "pics": sc.get("pics")})
                        ep["infl"] |= infl
                    known = data.get("words", {})
                    # Meanings from the PC's Yomitan dictionaries only (dicts.py).
                    pair = f"{LANG[key]}-{cfg.people[key].translation if key in cfg.people else 'en'}"
                    ep["pair"] = pair  # episode(): each scene's sense and the readings
                    ep["gloss"] = {i: dicts.look(pair, (known.get(i) or {}).get("lemma", ""), i) for i in ep["infl"]}
                    for i in ep["infl"]:
                        if (known.get(i) or {}).get("lemma"):
                            lemmas.setdefault(i, known[i]["lemma"])
                    episodes[ep["id"]] = ep
            self.episodes, self.words, self.lemmas = episodes, words, lemmas
            self.parsed = parsed
            self.scanned = (mtime, time.time())

    @staticmethod
    def profiles() -> list[dict]:
        """The people with kumapie, for the TV's "Who's watching?"; menus in Spanish for those whose meanings are."""
        def menu(libs: dict) -> str:
            return "es" if any(k in cfg.people and cfg.person(k).translation == "es" for k in libs.values()) else "en"
        return [{"id": p, "name": p.capitalize(), "menu": menu(libs)} for p, libs in sync.PEOPLE.items()]

    def person_of(self, eid: str = "", scene: str = "") -> str:
        """Whose an episode (or a scene's episode) is; the feed person's when unknown."""
        ep = self.episodes.get(eid) or next((e for e in self.episodes.values() if any(sc["id"] == scene for sc in e["scenes"])), None) \
            if scene else self.episodes.get(eid)
        return ep["person"] if ep else F["person"]

    @staticmethod
    def people(person: str | None) -> set[str]:
        """A sync person's libraries ("pedro" -> pedro-de, pedro-en, pedro-ja); none given: the feed person (the TV)."""
        return set(sync.PEOPLE.get(person or "", {}).values()) or {F["person"]}

    # ------------------------------------------------------------ JSON

    def info(self) -> dict:
        return {"api": API, "language": PERSON.transcript, "translation": PERSON.translation,
                "episodes": len(self.episodes)}

    # ------------------------------------------------------------ seen: the TV's own + the phones'

    @staticmethod
    def device_seen(body: dict) -> dict:
        """A person's phones' merged view: {"person": sync person, "seen": {scene: episode}, "unwatched": {episode: epoch}}."""
        person = str(body.get("person", ""))
        if person not in sync.PEOPLE:
            raise ValueError("unknown person")
        seen = {str(s): str(e) for s, e in (body.get("seen") or {}).items()}
        unwatched = {str(e): int(t) for e, t in (body.get("unwatched") or {}).items()}
        try:
            data = json.loads(DEVICE_SEEN.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[person] = {"seen": seen, "unwatched": unwatched}
        tmp = DEVICE_SEEN.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(DEVICE_SEEN)
        return {"ok": True, "seen": len(seen), "unwatched": len(unwatched)}

    @staticmethod
    def device_watch(body: dict) -> dict:
        """A person's phones' own watch time per language (the TV's left out): the TV's Home card adds it to its own."""
        person = str(body.get("person", ""))
        if person not in sync.PEOPLE:
            raise ValueError("unknown person")
        watch = {str(l): {str(d): float(s) for d, s in days.items()} for l, days in (body.get("watch") or {}).items()}
        try:
            data = json.loads(DEVICE_WATCH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[person] = watch
        tmp = DEVICE_WATCH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(DEVICE_WATCH)
        return {"ok": True, "langs": len(watch)}

    @staticmethod
    def seen_scenes(db) -> dict[str, set[str]]:
        """Episode -> scenes seen anywhere: the TV's, minus those before a "Mark as not watched", plus the phones'."""
        try:
            data = json.loads(DEVICE_SEEN.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        unwatched: dict[str, int] = {}
        for p in data.values():
            for e, t in p.get("unwatched", {}).items():
                unwatched[e] = max(t, unwatched.get(e, 0))
        out: dict[str, set[str]] = {}
        for scene, eid, last in db.execute("select scene, episode, last from seen"):
            if eid in unwatched and datetime.datetime.fromisoformat(last).timestamp() < unwatched[eid]:
                continue
            out.setdefault(eid, set()).add(scene)
        for p in data.values():
            for scene, eid in p.get("seen", {}).items():
                out.setdefault(eid, set()).add(scene)
        return out

    def shows(self, status_of, person: str | None = None) -> dict:
        """[status_of]: a library person's known words (None: their phone never sent any)."""
        self.synced()
        people = self.people(person)
        with self.tv_db() as db:
            resume = dict(db.execute("select episode, pos from progress"))
            resumed = {e: int(datetime.datetime.fromisoformat(u).timestamp())  # Continue watching: newest first
                       for e, u in db.execute("select episode, updated from progress") if u}
            seen = {e: len(s) for e, s in self.seen_scenes(db).items()}
        shows: dict[str, dict] = {}
        for ep in sorted(self.episodes.values(), key=lambda e: (e["season"] or 0, e["episode"] or 0, e["source"])):
            if ep["person"] not in people:
                continue
            sh = shows.setdefault(ep["show"], {"id": ep["show"], "title": ep["show_title"], "kind": ep["kind"],
                                               "lang": LANG.get(ep["person"], PERSON.transcript),
                                               "poster": f"/api/tv/poster/{ep['show']}.jpg?v=2", "episodes": []})
            # Levels from the known words the person's phone sent; none yet: no levels.
            status = status_of(ep["person"])
            levels = [self.level(sc, status) for sc in ep["scenes"] if sc["german"]] if status is not None else []
            sh["episodes"].append({
                "id": ep["id"], "title": ep["title"], "season": ep["season"], "episode": ep["episode"],
                "duration": ep["duration"], "thumb": f"/api/tv/thumb/{ep['id']}.jpg",
                "scenes": len(ep["scenes"]), "seen": seen.get(ep["id"], 0), "resume": resume.get(ep["id"]), "resumed_at": resumed.get(ep["id"], 0),
                "easy": sum(1 for lv in levels if lv <= 1)})  # scenes at i+0 / i+1 today
        self.fill_meta()
        lang = self.speaks(people)
        for sh in shows.values():
            # The poster's date in its address: a poster that appears or changes is fetched again (devices cache them)
            art = self.poster(sh["id"])
            sh["poster"] = f"/api/tv/poster/{sh['id']}.jpg?v={int(art.stat().st_mtime) if art else 0}"
            m = self.show_meta(sh["id"])
            if not m:
                continue
            if sh["kind"] in ("anime", "shows", "movies"):  # a channel or a novel keeps its own name (Nicos Weg, a chapter)
                sh["title"] = (m.get("title") or {}).get(lang) or (m.get("title") or {}).get("en") or sh["title"]
            sh["description"] = (m.get("description") or {}).get(lang) or (m.get("description") or {}).get("en") or ""
            sh["year"] = m.get("year")
            for e in sh["episodes"]:  # the apps show it only once the episode is watched (spoilers)
                if e["episode"]:
                    e["episode_title"] = (m.get("episodes") or {}).get(f"{e['season'] or 1}-{e['episode']}", "")
        rows = sorted(shows.values(), key=lambda s: (KINDS.index(s["kind"]) if s["kind"] in KINDS else 9, s["title"]))
        return {"shows": rows}

    # ------------------------------------------------------------ show metadata (items 20-23)

    @staticmethod
    def speaks(people: set[str]) -> str:
        """The language a person reads titles and descriptions in: their libraries' translation (Pedro: English)."""
        return next((cfg.people[k].translation for k in people if k in cfg.people and cfg.people[k].translation != "en"), "en")

    @staticmethod
    def show_meta(show: str) -> dict | None:
        try:
            m = json.loads((META / f"{show}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return None if m.get("none") else m

    @staticmethod
    def save_meta(show: str, kind: str, source: str, id_: str) -> dict:
        d = showmeta.details(source, id_, ("en", "es", "de", "ja"))
        META.mkdir(parents=True, exist_ok=True)
        if d.get("poster"):
            try:
                req = urllib.request.Request(d["poster"], headers={"User-Agent": showmeta.AGENT})
                with urllib.request.urlopen(req, timeout=30) as r:
                    (META / f"{show}.part").write_bytes(r.read())
                (META / f"{show}.part").replace(META / f"{show}.jpg")
            except OSError as e:
                log(f"meta: poster for {show}: {e}")
        (META / f"{show}.json").write_text(json.dumps({**d, "kind": kind}, ensure_ascii=False), encoding="utf-8")
        return d

    def set_meta(self, body: dict) -> dict:
        """"Fix match" on a phone for a PC show: {"show": folder, "kind", "source", "id"}."""
        show, kind = str(body.get("show", "")), str(body.get("kind", ""))
        if not re.fullmatch(r"[a-z0-9_]+", show) or kind not in META_KINDS.values():
            raise ValueError("bad show or kind")
        d = self.save_meta(show, kind, str(body["source"]), str(body["id"]))
        return {"ok": True, "title": d.get("title", {})}

    def fill_meta(self) -> None:
        """Looks up, once and in the background, every show without metadata (a phone's match first)."""
        if getattr(self, "_meta_busy", False):
            return
        todo = {}
        for ep in self.episodes.values():
            kind = META_KINDS.get(ep["kind"])
            if kind and ep["show"] not in todo and not (META / f"{ep['show']}.json").exists():
                year = re.search(r"_(\d{4})$", ep["show"])
                title = ep["show_title"] or ep["show"].replace("_", " ")
                if kind == "youtube":  # searched by one of its videos; PC-made ones without a link stay as they are
                    title = ep["source"] if str(ep.get("source", "")).startswith("http") else ""
                if title:
                    todo[ep["show"]] = (kind, title, int(year[1]) if year else None, ep.get("match"))
        if not todo:
            return
        self._meta_busy = True

        def run():
            try:
                for show, (kind, title, year, match) in todo.items():
                    try:
                        if match:
                            source, _, id_ = match.partition(":")
                        else:
                            query = title if kind == "youtube" else re.sub(r"\s*\(?\d{4}\)?$", "", title).strip()
                            found = showmeta.search(kind, query, year)
                            if not found:
                                META.mkdir(parents=True, exist_ok=True)
                                (META / f"{show}.json").write_text('{"none": true}', encoding="utf-8")
                                log(f"meta: no match for {show}")
                                continue
                            source, id_ = found[0]["source"], found[0]["id"]
                        d = self.save_meta(show, kind, source, id_)
                        log(f"meta: {show} -> {source} {id_} {d.get('title', {}).get('en')}")
                    except Exception as e:  # network: try again on a later request
                        log(f"meta: {show}: {e}")
                    time.sleep(1)  # be gentle with the free APIs
            finally:
                self._meta_busy = False
        threading.Thread(target=run, daemon=True).start()

    def episode(self, eid: str, status_of) -> dict | None:
        self.synced()
        ep = self.episodes.get(eid)
        if not ep:
            return None
        with self.tv_db() as db:
            row = db.execute("select pos from progress where episode = ?", (eid,)).fetchone()
            seen = self.seen_scenes(db).get(eid, set())
            looked = dict(db.execute("select word, count(*) from lookups where person = ? group by word", (ep["person"],)))
        status = status_of(ep["person"])
        fits = self.scene_senses(ep)
        marked = self.known_words() if ep["person"] == F["person"] else set()
        theirs = status is None  # their phone never sent its known words: plain on the TV
        if theirs:
            status = frozenset(ep["infl"])
        return {
            "id": eid, "show": ep["show_title"], "title": ep["title"], "duration": ep["duration"],
            "lang": LANG.get(ep["person"], PERSON.transcript), "novel": ep.get("novel", False),
            "video": f"/api/tv/video/{eid}", "resume": row[0] if row else None,
            "scenes": [{"id": sc["id"], "i": sc["i"], "start": sc["start"], "end": sc["end"],
                        "german": sc["german"], "level": self.level(sc, status) if sc["german"] and not theirs else None,
                        "seen": sc["id"] in seen,
                        "cues": [{"start": c["start"], "end": c["end"], "seg": c["seg"]} for c in sc["cues"]],
                        "english": sc["english"], "g": fits.get(sc["id"], {}),
                        **({"bgm": f"/api/tv/file/{eid}/{sc['bgm']}"} if sc.get("bgm") else {}),
                        # A novel kumapie draws (item 46): the screen at the line's start, then [start, fade, picture, mask].
                        **({"base": f"/api/tv/file/{eid}/{sc['base']}",
                            "pics": [[s, d, f"/api/tv/file/{eid}/{p}", m and f"/api/tv/file/{eid}/{m}"] for s, d, p, m in sc["pics"]]}
                           if sc.get("base") else {})}
                       for sc in ep["scenes"]],
            # s: k/u, g: meaning, lemma: dictionary form, n: times looked up, m: marked known on the TV
            "words": {i: {"s": "k" if i in status else "u", "g": g, "lemma": self.lemmas.get(i, ""),
                          "n": looked.get(i, 0), "m": i in marked, **self.reading(ep, i)}
                      for i, g in ep["gloss"].items()},
        }

    def scene_senses(self, ep: dict) -> dict[str, dict[str, str]]:
        """{scene: {word: sense}}: for an episode whose meanings come from dicts.py, each word's sense that fits the
        scene's English when it isn't the general gloss (the TV card shows a scene's own meaning first)."""
        pair = ep.get("pair")
        if not pair or not pair.endswith("-en"):
            return {}
        out: dict[str, dict[str, str]] = {}
        for sc in ep["scenes"]:
            english = " ".join(e[2] for e in sc["english"] if len(e) > 2)
            for i in sc["infl"]:
                _kana, ss = dicts.senses(pair, self.lemmas.get(i, ""), i)
                pick = dicts.best(ss, english) if len(ss) > 1 else None
                if pick and pick != ep["gloss"].get(i):
                    out.setdefault(sc["id"], {})[i] = pick
        return out

    def reading(self, ep: dict, word: str) -> dict:
        """{"r": kana} for a Japanese word written with kanji; {} otherwise."""
        if not ep.get("pair", "").startswith("ja-"):
            return {}
        lemma = self.lemmas.get(word, "")
        kana, _ = dicts.senses(ep["pair"], lemma, word)
        written = lemma or word
        return {"r": kana} if kana and kana != written and any("一" <= c <= "鿿" for c in written) else {}

    @staticmethod
    def level(sc: dict, status) -> int:
        """Distinct inflections in the scene that aren't known (as the feed counts)."""
        return sum(1 for i in sc["infl"] if i not in status)

    def video(self, eid: str) -> Path | None:
        self.synced()
        ep = self.episodes.get(eid)
        f = (ep.get("path") or cfg.library(F["person"]) / ep["source"]) if ep else None
        return f if f and f.is_file() else None

    def file(self, eid: str, rel: str) -> Path | None:
        """A file beside the episode's video (a novel's music, bgm/<name>.ogg)."""
        video = self.video(eid)
        f = video.parent / rel if video else None
        return f if f and f.is_file() else None

    # ------------------------------------------------------------ images

    def thumb(self, eid: str) -> Path | None:
        """A frame a third into the episode (past the opening), 480 px wide, made once."""
        ep, video = self.episodes.get(eid), self.video(eid)
        if not video:
            return None
        out = CACHE / f"thumb_{eid}.jpg"
        with self.thumb_lock:
            if not out.exists():
                CACHE.mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(".part.jpg")
                r = subprocess.run([self.ffmpeg, "-v", "error", "-y", "-ss", f"{(ep['duration'] or 60) / 3:.1f}",
                                    "-i", str(video), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(tmp)],
                                   capture_output=True)
                if r.returncode == 0 and tmp.exists():
                    tmp.replace(out)
        return out if out.exists() else None

    def poster(self, show: str) -> Path | None:
        self.synced()  # a fresh server knows the synced shows only after this
        eps = [e for e in self.episodes.values() if e["show"] == show]
        if not eps:
            return None
        ep = min(eps, key=lambda e: (e["season"] or 0, e["episode"] or 0))
        folder = ep["path"].parent if "path" in ep else cfg.library(ep["person"]) / ep["kind"] / show
        for name in ("cover.jpg", "poster.jpg"):
            if (folder / name).is_file():
                return folder / name
        if (META / f"{show}.jpg").is_file():
            return META / f"{show}.jpg"
        return None  # no cover: the apps draw a neutral poster (an episode frame could spoil, item 24)

    # ------------------------------------------------------------ progress

    def progress(self, body: dict) -> dict:
        eid = str(body.get("episode", ""))
        self.synced()
        ep = self.episodes.get(eid)
        # A phone's own episode not on the PC (yet): its position is kept for when it arrives.
        if not ep and not sync.ID.match(eid):
            raise ValueError("unknown episode")
        ids = {sc["id"] for sc in ep["scenes"]} if ep else set()
        seen = [s for s in body.get("seen", []) if s in ids]
        watched = max(0.0, min(float(body.get("watched", 0) or 0), 600.0))  # one report covers at most 10 min
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self.write_lock, self.tv_db() as db:
            if body.get("pos") is not None:
                db.execute("insert or replace into progress values (?, ?, ?)", (eid, float(body["pos"]), now))
            for s in seen:
                db.execute("insert into seen values (?, ?, ?, ?, 1) on conflict (scene) do update"
                           " set last = excluded.last, times = times + 1", (s, eid, now, now))
            if watched:
                db.execute("insert into watch_p values (?, ?, ?) on conflict (day, person) do update"
                           " set seconds = seconds + excluded.seconds", (study_day(), self.person_of(eid), watched))
        return {"ok": True}

    def lookup(self, body: dict) -> dict:
        word = str(body.get("word", ""))[:100]
        if not word:
            raise ValueError("no word")
        with self.write_lock, self.tv_db() as db:
            scene = str(body.get("scene", ""))[:20]
            person = self.person_of(scene=scene)
            db.execute("insert into lookups values (?, ?, ?, ?)",
                       (word, scene, datetime.datetime.now().isoformat(timespec="seconds"), person))
            n = db.execute("select count(*) from lookups where word = ? and person = ?", (word, person)).fetchone()[0]
        # Every sense, for the PC page's word card (the TV and phones show their own)
        ep = next((e for e in self.episodes.values() if any(sc["id"] == scene for sc in e["scenes"])), None) if scene else None
        _kana, ss = dicts.senses(ep["pair"], self.lemmas.get(word, ""), word) if ep and ep.get("pair") else ("", [])
        return {"ok": True, "n": n, "senses": ss}

    # ------------------------------------------------------------ words marked known

    @staticmethod
    def known_words() -> set[str]:
        try:
            return {w.strip() for w in KNOWN_WORDS.read_text(encoding="utf-8").splitlines()
                    if w.strip() and not w.startswith("#")}
        except FileNotFoundError:
            return set()

    def mark_known(self, word: str, known: bool) -> None:
        """Adds or removes the word in the known-words file (marked known on the TV)."""
        if not word or "\n" in word or word.startswith("#"):
            raise ValueError("bad word")
        with self.write_lock:
            words = self.known_words()
            words = words | {word} if known else words - {word}
            KNOWN_WORDS.parent.mkdir(parents=True, exist_ok=True)
            tmp = KNOWN_WORDS.with_name(KNOWN_WORDS.name + ".part")
            tmp.write_text("# words marked known on the TV (kumapie): the feed, the TV and morphs recalc count them as known\n"
                           + "".join(f"{w}\n" for w in sorted(words)), encoding="utf-8")
            tmp.replace(KNOWN_WORDS)

    def history(self, person: str | None = None) -> dict:
        """Everything the TV (and the phone, before 2026-10-06) recorded, in kumapie's progress format. Another
        person's: their episodes' positions and scenes, their time and lookups (words marked known are the feed person's)."""
        self.synced()
        people = self.people(person)
        mine = {e for e, ep in self.episodes.items() if ep["person"] in people}
        def epoch(iso: str) -> int:
            return int(datetime.datetime.fromisoformat(iso).timestamp())
        with self.tv_db() as db:
            pos = {e: [p, epoch(u)] for e, p, u in db.execute("select episode, pos, updated from progress")}
            seen = {s: [e, epoch(f), epoch(l), n] for s, e, f, l, n in db.execute("select scene, episode, first, last, times from seen")}
            q = ",".join("?" * len(people))
            watch = dict(db.execute(f"select day, sum(seconds) from watch_p where person in ({q}) group by day", tuple(people)))
            lookups = dict(db.execute(f"select word, count(*) from lookups where person in ({q}) group by word", tuple(people)))
            lwatch: dict[str, dict[str, float]] = {}
            for p, day, s in db.execute(f"select person, day, seconds from watch_p where person in ({q})", tuple(people)):
                days = lwatch.setdefault(LANG.get(p, PERSON.transcript), {})
                days[day] = days.get(day, 0) + s
        if F["person"] not in people:
            pos = {e: v for e, v in pos.items() if e in mine}
            seen = {s: v for s, v in seen.items() if v[0] in mine}
            return {"pos": pos, "seen": seen, "watch": watch, "lwatch": lwatch, "lookups": lookups, "marked": {}, "gloss": {}}
        return {"pos": pos, "seen": seen, "watch": watch, "lwatch": lwatch, "lookups": lookups,
                "marked": {w: [True, 0] for w in self.known_words()},
                "gloss": {w: g for w in lookups if (g := self.dict_gloss(w))}}

    def dict_gloss(self, word: str) -> str:
        """A looked-up word's dictionary meaning (kana or kanji: Japanese, else German)."""
        pair = "ja-en" if any("぀" <= c <= "鿿" for c in word) else "de-en"
        return dicts.look(pair, self.lemmas.get(word, ""), word)

    def stats(self, person: str | None = None) -> dict:
        """Watch time per day with today / 7 days / total and the streak, scenes seen, lookups, words marked."""
        people = self.people(person)
        self.synced()
        q = ",".join("?" * len(people))
        with self.tv_db() as db:
            days = dict(db.execute(f"select day, round(sum(seconds)) from watch_p where person in ({q}) group by day order by day", tuple(people)))
            seen = dict(db.execute("select episode, count(*) from seen group by episode"))
            lookups = db.execute(f"select count(*), count(distinct word) from lookups where person in ({q})", tuple(people)).fetchone()
            top = db.execute(f"select word, count(*) n from lookups where person in ({q}) group by word order by n desc, word limit 10",
                             tuple(people)).fetchall()
        today = datetime.date.fromisoformat(study_day())
        week = sum(days.get((today - datetime.timedelta(days=i)).isoformat(), 0) for i in range(7))
        streak, day = 0, today
        if days.get(day.isoformat(), 0) < 60:  # today not started yet: the streak still counts up to yesterday
            day -= datetime.timedelta(days=1)
        while days.get(day.isoformat(), 0) >= 60:  # a day counts with at least a minute watched
            streak += 1
            day -= datetime.timedelta(days=1)
        gloss = {}
        mine = [ep for ep in self.episodes.values() if ep["person"] in people]
        for ep in mine:
            gloss.update(ep["gloss"])
        shows: dict[str, dict] = {}
        for ep in mine:
            sh = shows.setdefault(ep["show"], {"title": ep["show_title"], "seen": 0, "scenes": 0})
            sh["seen"] += seen.get(ep["id"], 0)
            sh["scenes"] += len(ep["scenes"])
        return {
            "days": days, "today": days.get(today.isoformat(), 0), "week": week, "total": sum(days.values()),
            "streak": streak,
            "scenes_seen": sum(seen.get(e["id"], 0) for e in mine), "scenes_total": sum(len(e["scenes"]) for e in mine),
            "lookups": lookups[0], "words_looked_up": lookups[1],
            "top_lookups": [[w, n, gloss.get(w, "")] for w, n in top],
            "marked_known": len(self.known_words()) if F["person"] in people else 0,
            "shows": sorted(shows.values(), key=lambda x: -x["seen"]),
            "langs": self.lang_totals(people, today),
        }

    def lang_totals(self, people: set[str], today: datetime.date) -> dict:
        """Per language: time on the TV plus the person's phones (today, 7 days, all) and the words their phone knows."""
        try:
            phones = json.loads(DEVICE_WATCH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            phones = {}
        q = ",".join("?" * len(people))
        with self.tv_db() as db:
            rows = db.execute(f"select person, day, seconds from watch_p where person in ({q})", tuple(people)).fetchall()
        out = {}
        for key in people:
            lang = LANG.get(key, PERSON.transcript)
            owner = next((p for p, libs in sync.PEOPLE.items() if key in libs.values()), "")
            days = dict(phones.get(owner, {}).get(lang, {}))
            for p, day, s in rows:
                if p == key:
                    days[day] = days.get(day, 0) + s
            out[lang] = {"today": days.get(today.isoformat(), 0),
                         "week": sum(days.get((today - datetime.timedelta(days=i)).isoformat(), 0) for i in range(7)),
                         "total": sum(days.values()), "known": len(known.words(key) or ())}
        return out
