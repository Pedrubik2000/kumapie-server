"""German feed: cut Pedro's library into short clips and gloss their words (plan: german_feed_plan.md).

  feed.py build [--only TEXT]       group subtitle cues into 5-15 s clips, parse them, cut them with
                                    ffmpeg into state\\feed\\clips\\, index them in state\\feed\\feed.db
  feed.py gloss --estimate          clips and words still without glosses, and what glossing them costs
  feed.py gloss --limit 20          gloss a few clips (random) and print the result, to check the prompt
  feed.py gloss                     gloss every clip that has none yet (paid: ask Pedro first)
  feed.py show [ID]                 print a clip (random if no id) as the service sees it
  feed.py scenes [--only TEXT] [--print]
                                    split whole episodes into scenes for the TV app (kumapie-tv), no cutting
  feed.py export DIR [--scenes]     clips (or scenes) still without glosses, as JSON batches to gloss in a session
  feed.py export-defs DIR [--only TEXT]
                                    per-line definitions for the TV (definitions v2): the red/orange words of
                                    every line still without one, by scene, to write in a session
  feed.py import-defs FILES         load them (parses each German definition into words, for colours)
  feed.py export-words DIR [--only TEXT]
                                    the TV's words with no meaning anywhere yet (one meaning per word), with
                                    their lines, as JSON batches to write in a session; load with import-words

Run with .venv (spaCy, anthropic); another Python re-runs it with that one. Resumable and safe to stop: finished clips are kept and skipped.
Settings in config.toml [feed]. The service (services\\feed\\server.py) only reads feed.db.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

CODE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE))
from dojo import cfg  # noqa: E402

F = cfg.raw["feed"]
PERSON = F["person"]
STATE = cfg.state / "feed"
CLIPS = STATE / "clips"
DB = STATE / "feed.db"
PROMPT = Path(__file__).parent / "prompts" / "gloss_v1.md"
WORD = re.compile(r"\w+")
PRICE = {"claude-haiku-4-5": (1.0, 5.0)}  # $ per million input / output tokens


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def need(module: str, python: Path) -> None:
    """Re-run this command with `python` when `module` cannot be imported here."""
    try:
        __import__(module)
        return
    except ImportError:
        pass
    if not python.exists() or Path(sys.executable).resolve() == python.resolve():
        sys.exit(f"feed: `{module}` is not installed for {sys.executable}")
    sys.exit(subprocess.call([str(python), __file__, *sys.argv[1:]]))


# ---------------------------------------------------------------- database

SCHEMA = """
create table if not exists meta (key text primary key, value text);
create table if not exists clips (
    id text primary key, source text, title text, start real, end real,
    english text, words integer, built integer default 0);
create table if not exists cues (
    clip_id text, idx integer, start real, end real, text text, tokens text,
    primary key (clip_id, idx));
create table if not exists glosses (
    clip_id text, inflection text, gloss text, model text, prompt text,
    primary key (clip_id, inflection));
create table if not exists line_defs (
    scene_id text, cue_idx integer, inflection text, lemma text, english text, german text, grammar text,
    german_tokens text, source text, primary key (scene_id, cue_idx, inflection));
create table if not exists word_glosses (
    inflection text primary key, gloss text, model text, prompt text);
create table if not exists episodes (
    source text primary key, kind text, show text, show_title text, title text,
    season integer, episode integer, duration real);
create table if not exists scenes (
    id text primary key, source text, idx integer, start real, end real, german integer, english text);
create index if not exists scenes_source on scenes (source, idx);
create table if not exists scene_cues (
    scene_id text, idx integer, start real, end real, text text, tokens text,
    primary key (scene_id, idx));
"""


def connect() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=60)
    db.execute("pragma journal_mode = wal")  # the service reads while a build writes
    db.executescript(SCHEMA)
    return db


# ---------------------------------------------------------------- clips from subtitles

@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Clip:
    source: Path  # the video
    cues: list[Cue]
    start: float = 0.0  # padded
    end: float = 0.0
    english: str = ""
    id: str = ""
    words: int = 0
    title: str = ""
    tokens: list[list] = field(default_factory=list)  # per cue


def read_srt(path: Path) -> list[Cue]:
    cues = []
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            m = re.match(r"(\d+):(\d\d):(\d\d)[,.](\d{3}) --> (\d+):(\d\d):(\d\d)[,.](\d{3})", line)
            if m:
                t = [int(x) for x in m.groups()]
                sec = lambda h, mi, s, ms: h * 3600 + mi * 60 + s + ms / 1000
                body = " ".join(x.strip() for x in lines[i + 1:] if x.strip())
                body = re.sub(r"<[^>]+>|\{\\[^}]*}", "", body).strip()
                if body:
                    start, end = sec(*t[:4]), sec(*t[4:])
                    if end - start > 8 and end - start > 0.3 * len(body):
                        # A transcription glitch: the last word held over the whole ending song (93 s
                        # for "los?"). Cut it to a normal length for its text.
                        end = start + max(1.5, 0.1 * len(body))
                    cues.append(Cue(start, end, body))
                break
    return sorted(cues, key=lambda c: c.start)


def split_run(run: list[Cue], max_secs: float) -> list[list[Cue]]:
    """A run of cues without long pauses -> pieces of at most max_secs, cut at the biggest pauses
    (a pause after a sentence end counts a second more), so pieces stay whole exchanges."""
    if len(run) == 1 or run[-1].end - run[0].start <= max_secs:
        return [run]
    best = max(range(1, len(run)),
               key=lambda i: run[i].start - run[i - 1].end + (1.0 if run[i - 1].text.rstrip()[-1:] in ".!?" else 0)
               - 0.001 * abs(i - len(run) / 2))
    return split_run(run[:best], max_secs) + split_run(run[best:], max_secs)


def title_of(video: Path, library: Path) -> str:
    rel = video.relative_to(library)
    folder = rel.parts[1] if len(rel.parts) > 2 else video.stem
    name = F.get("titles", {}).get(folder) or re.sub(r"_\d{4}$", "", folder).replace("_", " ").title()
    ep = re.search(r"_s(\d+)_(\d+)$", video.stem)
    if ep:
        name += f" · E{int(ep.group(2))}" if ep.group(1) == "1" else f" · S{int(ep.group(1))}E{int(ep.group(2))}"
    return name


def german(text: str, english: str) -> bool:
    """False for lines that are not German speech: song lyrics in Japanese (non-Latin script), or
    English speech the transcript kept as is (the line is the same as its translation)."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters or sum(ch.isascii() or ch in "äöüÄÖÜß" for ch in letters) < 0.8 * len(letters):
        return False
    norm = lambda s: re.sub(r"\W+", " ", s.lower()).strip()
    return difflib.SequenceMatcher(None, norm(text), norm(english)).ratio() < 0.8


def exchanges(cues: list[Cue]) -> list[tuple[list[Cue], float, float]]:
    """Every cue grouped into exchanges: (cues, padded start, padded end). Runs of cues with gaps up to
    [feed] max_gap, split to max_secs; the padding never reaches into a neighbouring exchange."""
    runs: list[list[Cue]] = []
    for c in cues:
        if runs and c.start - runs[-1][-1].end <= F["max_gap"]:
            runs[-1].append(c)
        else:
            runs.append([c])
    pieces = [p for r in runs for p in split_run(r, F["max_secs"])]
    out = []
    for i, p in enumerate(pieces):
        before = pieces[i - 1][-1].end if i else 0.0
        after = pieces[i + 1][0].start if i + 1 < len(pieces) else p[-1].end + F["pad"]
        out.append((p, round(max(p[0].start - F["pad"], before, 0.0), 3), round(min(p[-1].end + F["pad"], after), 3)))
    return out


def clips_of(video: Path, library: Path) -> list[Clip]:
    cues = read_srt(video.with_suffix(".srt"))
    en_path = video.with_name(f"{video.stem}.{cfg.person(PERSON).translation}.srt")
    english = read_srt(en_path) if en_path.exists() else []
    title = title_of(video, library)
    source = video.relative_to(library).as_posix()
    out = []
    for p, start, end in exchanges(cues):
        words = sum(len(WORD.findall(c.text)) for c in p)
        if words < F["min_words"]:
            continue
        en = [e.text for e in english if p[0].start <= (e.start + e.end) / 2 <= p[-1].end]
        en = [re.sub(r"\s*\([^)]*\)", "", e).strip() for e in en]  # "(ausgelaugt)": the German, shown anyway
        if not german(" ".join(c.text for c in p), " ".join(en)):
            continue
        cid = hashlib.sha1(f"{source}|{start:.3f}|{end:.3f}".encode()).hexdigest()[:12]
        out.append(Clip(video, p, start, end, " ".join(en), cid, words, title))
    return out


# ---------------------------------------------------------------- parsing (spaCy, as the Anki tags)

def make_parser():
    sys.path.insert(0, str(cfg.kumapie_app / "mobile" / "src" / "main" / "python"))
    from morphemizer import VERB_POS, Morphemizer  # the phones' own copy

    m = cfg.raw["morphs"]
    mz = Morphemizer(m["spacy_model"], **{k: m[k] for k in ("ignore_names", "ignore_numbers", "ignore_bracket_contents",
                                                              "join_separable_verbs", "parse_lowercase") if k in m})
    # 1: the old morphs cache VERSION, kept so the settings hash (and the parsed clips) stay valid.
    settings = hashlib.sha1(json.dumps([1, mz.model, mz.ignore_names, mz.ignore_numbers,
                                        mz.ignore_bracket_contents, mz.join_separable_verbs,
                                        mz.parse_lowercase, sorted(mz.names)]).encode()).hexdigest()

    def parse(texts: list[str]) -> list[list[list]]:
        """Cue texts -> per cue [[surface, offset, inflection or None, lemma or None], ...].
        offset is where the surface starts in the cue text (-1 if it cannot be found)."""
        out = []
        for text, doc in zip(texts, mz.nlp.pipe([mz.prepare(t) for t in texts], batch_size=256)):
            toks = mz.tokens(doc)
            # The particle of a separable verb is shown with its verb's morph ("rufe ... an").
            for t, tok in zip(doc, list(toks)):
                if mz.join_separable_verbs and t.dep_ == "svp" and t.head.pos_ in VERB_POS and t.head.i != t.i:
                    toks[t.i] = type(tok)(tok.text, tok.lemma, tok.pos, tok.dep, toks[t.head.i].morph, tok.skipped)
            low, pos, row = text.lower(), 0, []
            for tok in toks:
                at = low.find(tok.text.lower(), pos)
                if at >= 0:
                    surface, pos = text[at:at + len(tok.text)], at + len(tok.text)
                else:
                    surface = tok.text
                m = tok.morph
                row.append([surface, at, m.inflection if m else None, m.lemma if m else None])
            out.append(row)
        return out

    return parse, settings


# ---------------------------------------------------------------- cutting

def ffmpeg() -> str:
    return os.environ.get("FFMPEG") or cfg.which("ffmpeg")


# Bump when the clip encoding changes: build re-cuts every clip, and the service adds ?v=<this> to clip
# URLs so phones don't keep the old copies (they are cached as immutable).
# 2: Firefox on Android froze the picture of clips with one keyframe (only keyframes were drawn), so:
#    no B-frames, Main profile, a keyframe every 2 s, both streams starting at 0 (no edit list).
# 3: every clip's audio loudness-normalized to [feed] loudness (the word audio's level): the YouTube
#    video was ~9 dB louder than the word audio, anime and the movie 3-10 dB quieter.
ENCODE = 3


def loudness_filter(clip: Clip, library: Path) -> str:
    """Two-pass EBU R128 (as tools
ormalize_audio.py): measure the clip's audio, then a linear gain
    to [feed] loudness (true peak -1.5 dB). Silent clips are left alone."""
    I, tp, lra = F.get("loudness", -22), -1.5, 11
    r = subprocess.run(
        [ffmpeg(), "-hide_banner", "-ss", f"{clip.start:.3f}", "-i", str(library / clip.source),
         "-t", f"{clip.end - clip.start:.3f}", "-vn", "-sn", "-dn", "-map", "0:a:0?",
         "-af", f"loudnorm=I={I}:TP={tp}:LRA={lra}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = r.stderr
    try:
        stats = json.loads(err[err.rindex("{"):err.rindex("}") + 1])
    except ValueError:
        return "asetpts=PTS-STARTPTS"
    if stats.get("input_i") in (None, "-inf"):
        return "asetpts=PTS-STARTPTS"
    return (f"asetpts=PTS-STARTPTS,loudnorm=I={I}:TP={tp}:LRA={lra}:linear=true"
            f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}")


def cut(clip: Clip, library: Path) -> str:
    """Always encodes: callers only pass clips that are not built (new, or an older ENCODE)."""
    mp4, jpg = CLIPS / f"{clip.id}.mp4", CLIPS / f"{clip.id}.jpg"
    tmp = CLIPS / f"{clip.id}.part.mp4"
    subprocess.run(
        [ffmpeg(), "-v", "error", "-y", "-ss", f"{clip.start:.3f}", "-i", str(library / clip.source),
         "-t", f"{clip.end - clip.start:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
         "-vf", f"scale=-2:{F['height']},setpts=PTS-STARTPTS", "-af", loudness_filter(clip, library), "-ar", "48000",
         "-c:v", "libx264", "-profile:v", "main", "-level", "3.1", "-bf", "0", "-g", "48", "-keyint_min", "24",
         "-crf", str(F["crf"]), "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(tmp)],
        check=True, capture_output=True)
    tmp.replace(mp4)
    tmpj = CLIPS / f"{clip.id}.part.jpg"
    subprocess.run([ffmpeg(), "-v", "error", "-y", "-i", str(mp4), "-frames:v", "1", "-q:v", "5", str(tmpj)],
                   check=True, capture_output=True)
    tmpj.replace(jpg)
    return clip.id


# ---------------------------------------------------------------- build

def cmd_build(args) -> None:
    need(cfg.raw["morphs"]["spacy_model"], cfg.python)  # spaCy with the German model
    library = cfg.library(PERSON)
    videos = sorted(v for v in library.rglob("*.mp4") if v.with_suffix(".srt").exists())
    if args.only:
        videos = [v for v in videos if args.only.lower() in v.as_posix().lower()]
    log(f"{len(videos)} videos with subtitles in {library}")
    clips = [c for v in videos for c in clips_of(v, library)]
    wanted = {c.id: c for c in clips}
    log(f"{len(clips)} clips")

    db = connect()
    parse, settings = make_parser()
    if db.execute("select value from meta where key = 'parse'").fetchone() != (settings,):
        db.execute("delete from cues")  # morphemizer settings changed: parse everything again
        db.execute("insert or replace into meta values ('parse', ?)", (settings,))
    if db.execute("select value from meta where key = 'encode'").fetchone() != (str(ENCODE),):
        db.execute("update clips set built = 0")  # re-cut everything; the service serves built clips only
        db.execute("insert or replace into meta values ('encode', ?)", (str(ENCODE),))
        db.commit()
        log(f"clip encoding changed to version {ENCODE}: re-cutting every clip")
    have = {r[0] for r in db.execute("select distinct clip_id from cues")}
    known = {r[0] for r in db.execute("select id from clips")}

    # Gone: source removed or re-timed (only within what this run looked at, with --only).
    scope = {v.relative_to(library).as_posix() for v in videos} if args.only else None
    gone = [cid for cid, src in db.execute("select id, source from clips")
            if cid not in wanted and (scope is None or src in scope)]
    scene_ids = {r[0] for r in db.execute("select id from scenes")}
    for cid in gone:
        for t in ("clips where id", "cues where clip_id") + (() if cid in scene_ids else ("glosses where clip_id",)):
            db.execute(f"delete from {t} = ?", (cid,))
        for ext in ("mp4", "jpg"):
            (CLIPS / f"{cid}.{ext}").unlink(missing_ok=True)
    if gone:
        log(f"removed {len(gone)} clips that no longer exist")

    todo = [c for c in clips if c.id not in have]
    if todo:
        log(f"parsing {len(todo)} clips")
        flat = [cue.text for c in todo for cue in c.cues]
        parsed = iter(parse(flat))
        for c in todo:
            c.tokens = [next(parsed) for _ in c.cues]
            db.execute("insert or replace into clips (id, source, title, start, end, english, words, built)"
                       " values (?, ?, ?, ?, ?, ?, ?, ?)",
                       (c.id, c.source.relative_to(library).as_posix(), c.title, c.start, c.end, c.english,
                        c.words, int((CLIPS / f"{c.id}.mp4").exists() and (CLIPS / f"{c.id}.jpg").exists())))
            db.executemany("insert or replace into cues values (?, ?, ?, ?, ?, ?)",
                           [(c.id, i, round(cue.start - c.start, 3), round(cue.end - c.start, 3), cue.text,
                             json.dumps(toks, ensure_ascii=False))
                            for i, (cue, toks) in enumerate(zip(c.cues, c.tokens))])
    for c in clips:  # title/English may change without a re-time (config titles, a fixed .en.srt)
        if c.id in known:
            db.execute("update clips set title = ?, english = ? where id = ?", (c.title, c.english, c.id))
    db.commit()

    unbuilt = [wanted[r[0]] for r in db.execute("select id from clips where built = 0") if r[0] in wanted]
    try:  # today's feed clips first, so the page has them back soonest
        pool = in_pool(db)
        unbuilt.sort(key=lambda c: c.id not in pool)
    except (OSError, ValueError):
        pass
    CLIPS.mkdir(parents=True, exist_ok=True)
    for f in CLIPS.glob("*.part.*"):
        f.unlink()
    log(f"cutting {len(unbuilt)} clips with {F['jobs']} ffmpeg jobs")
    done = failed = 0
    start = time.time()
    with ThreadPoolExecutor(F["jobs"]) as pool:
        futures = {pool.submit(cut, c, library): c for c in unbuilt}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                fut.result()
                db.execute("update clips set built = 1 where id = ?", (c.id,))
                done += 1
            except subprocess.CalledProcessError as e:
                failed += 1
                log(f"ffmpeg failed for {c.id} ({c.source.name} {c.start:.1f}s): {e.stderr.decode(errors='replace')[-300:]}")
            if done % 25 == 0:
                db.commit()
                rate = done / max(time.time() - start, 1)
                log(f"  {done}/{len(unbuilt)} cut, {(len(unbuilt) - done - failed) / max(rate, 1e-6) / 60:.0f} min left")
    db.commit()

    ids = {r[0] for r in db.execute("select id from clips")}
    for f in CLIPS.iterdir():
        if f.name.split(".")[0] not in ids:
            f.unlink()
    size = sum(f.stat().st_size for f in CLIPS.iterdir()) / 1e9
    n = db.execute("select count(*), sum(built) from clips").fetchone()
    log(f"done: {n[0]} clips, {n[1] or 0} cut ({size:.2f} GB), {failed} failed")


# ---------------------------------------------------------------- scenes (kumapie-tv, plan: kumapie_tv_plan.md)
# Whole episodes split into scenes for the TV app: the same exchanges() as the clips, but every one is
# kept (short ones, and non-German ones flagged german = 0). A scene's id is made like a clip's, so a feed
# clip IS a scene and its glosses serve both. Cue and English times are episode times (not relative), as
# the TV plays the whole file and seeks. Nothing is cut.

def episode_of(video: Path, library: Path) -> tuple:
    """(kind, show folder, show title, episode title, season, episode) of a video."""
    rel = video.relative_to(library)
    show = rel.parts[1] if len(rel.parts) > 2 else video.stem
    show_title = re.sub(r" · .*", "", title_of(video, library))
    ep = re.search(r"_s(\d+)_(\d+)$", video.stem)
    season, number = (int(ep.group(1)), int(ep.group(2))) if ep else (None, None)
    title = (f"Episode {number}" if season == 1 else f"S{season} E{number}") if ep else ""
    return rel.parts[0], show, show_title, title, season, number


def duration_of(video: Path) -> float:
    r = subprocess.run([cfg.which("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                        str(video)], capture_output=True, text=True)
    return round(float(r.stdout.strip() or 0), 3)


def scenes_of(video: Path, library: Path) -> list[tuple]:
    """[(id, idx, start, end, german, [english [start, end, text]], cues)] for every exchange of a video."""
    cues = read_srt(video.with_suffix(".srt"))
    en_path = video.with_name(f"{video.stem}.{cfg.person(PERSON).translation}.srt")
    english = read_srt(en_path) if en_path.exists() else []
    source = video.relative_to(library).as_posix()
    out = []
    for i, (p, start, end) in enumerate(exchanges(cues)):
        en = [[round(e.start, 3), round(e.end, 3), re.sub(r"\s*\([^)]*\)", "", e.text).strip()]
              for e in english if p[0].start <= (e.start + e.end) / 2 <= p[-1].end]
        is_german = german(" ".join(c.text for c in p), " ".join(e[2] for e in en))
        sid = hashlib.sha1(f"{source}|{start:.3f}|{end:.3f}".encode()).hexdigest()[:12]
        out.append((sid, i, start, end, int(is_german), en, p))
    return out


def cmd_scenes(args) -> None:
    need(cfg.raw["morphs"]["spacy_model"], cfg.python)
    library = cfg.library(PERSON)
    videos = sorted(v for v in library.rglob("*.mp4") if v.with_suffix(".srt").exists())
    if args.only:
        videos = [v for v in videos if args.only.lower() in v.as_posix().lower()]
    db = connect()
    parse, settings = make_parser()
    if db.execute("select value from meta where key = 'scene_parse'").fetchone() != (settings,):
        db.execute("delete from scene_cues")
        db.execute("insert or replace into meta values ('scene_parse', ?)", (settings,))
    have = {r[0] for r in db.execute("select distinct scene_id from scene_cues")}
    clip_ids = {r[0] for r in db.execute("select id from clips")}

    wanted: dict[str, tuple] = {}
    sources = set()
    for v in videos:
        source = v.relative_to(library).as_posix()
        sources.add(source)
        row = db.execute("select duration from episodes where source = ?", (source,)).fetchone()
        duration = row[0] if row and row[0] else duration_of(v)  # titles are refreshed every run, the length once
        db.execute("insert or replace into episodes values (?, ?, ?, ?, ?, ?, ?, ?)",
                   (source, *episode_of(v, library), duration))
        for s in scenes_of(v, library):
            wanted[s[0]] = (source, *s)
    log(f"{len(videos)} videos, {len(wanted)} scenes ({sum(w[5] for w in wanted.values())} German)")

    # Gone: videos removed or re-timed (only within what this run looked at, with --only).
    gone_src = [] if args.only else [r[0] for r in db.execute("select source from episodes") if r[0] not in sources]
    for src in gone_src:
        db.execute("delete from episodes where source = ?", (src,))
    gone = [sid for sid, src in db.execute("select id, source from scenes")
            if sid not in wanted and (src in sources or src in gone_src)]
    for sid in gone:
        db.execute("delete from scenes where id = ?", (sid,))
        db.execute("delete from scene_cues where scene_id = ?", (sid,))
        if sid not in clip_ids:
            db.execute("delete from glosses where clip_id = ?", (sid,))
    if gone:
        log(f"removed {len(gone)} scenes that no longer exist")

    for sid, (source, _, idx, start, end, is_german, en, cues) in wanted.items():
        db.execute("insert or replace into scenes values (?, ?, ?, ?, ?, ?, ?)",
                   (sid, source, idx, start, end, is_german, json.dumps(en, ensure_ascii=False)))
    todo = [w for sid, w in wanted.items() if sid not in have]
    if todo:
        german_texts = [c.text for w in todo if w[5] for c in w[7]]
        log(f"parsing {len(german_texts)} lines of {len(todo)} scenes")
        parsed = iter(parse(german_texts))
        for source, sid, idx, start, end, is_german, en, cues in todo:
            db.executemany("insert or replace into scene_cues values (?, ?, ?, ?, ?, ?)",
                           [(sid, i, round(c.start, 3), round(c.end, 3), c.text,
                             json.dumps(next(parsed) if is_german else [], ensure_ascii=False))
                            for i, c in enumerate(cues)])
    db.commit()
    n = db.execute("select count(*), sum(german), count(distinct source) from scenes").fetchone()
    shared = db.execute("select count(*) from scenes join clips using (id)").fetchone()[0]
    log(f"done: {n[0]} scenes ({n[1]} German) in {n[2]} videos; {shared} are also feed clips")
    if args.print:
        print_scenes(db, args.only)


def print_scenes(db, only: str | None) -> None:
    for source, show, title, duration in db.execute(
            "select source, show_title, title, duration from episodes where source like ? order by source",
            (f"%{only or ''}%",)):
        print(f"\n{show} {title}  ({source}, {duration / 60:.1f} min)")
        for sid, idx, start, end, is_german in db.execute(
                "select id, idx, start, end, german from scenes where source = ? order by idx", (source,)):
            lines = [t for (t,) in db.execute("select text from scene_cues where scene_id = ? order by idx", (sid,))]
            mark = " " if is_german else "x"
            print(f"  {idx:3d} {mark} {int(start // 60):3d}:{start % 60:04.1f} {end - start:5.1f}s  " + " / ".join(lines)[:110])


# ---------------------------------------------------------------- glosses

def clip_words(db, cid: str) -> tuple[list[str], dict[str, str]]:
    """German lines of a clip or scene and its distinct morphs {inflection: lemma}, in order."""
    lines, words = [], {}
    rows = (db.execute("select text, tokens from cues where clip_id = ? order by idx", (cid,)).fetchall()
            or db.execute("select text, tokens from scene_cues where scene_id = ? order by idx", (cid,)).fetchall())
    for text, toks in rows:
        lines.append(text)
        for surface, _, infl, lemma in json.loads(toks):
            if infl:
                words.setdefault(infl, lemma)
    return lines, words


def gloss_prompt(title: str, lines: list[str], english: str, words: dict[str, str]) -> str:
    listed = "\n".join(f"- {w}" + (f" (from {l})" if " " in w else "") for w, l in words.items())
    return (PROMPT.read_text(encoding="utf-8")
            .replace("{show}", re.sub(r" · .*", "", title))
            .replace("{lines}", "\n".join(lines))
            .replace("{english}", english or "(none)")
            .replace("{words}", listed))


def parse_reply(text: str, words: dict[str, str]) -> dict[str, str]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    lower = {k.lower().strip(): v for k, v in data.items() if isinstance(v, str)}
    return {w: lower[w].strip() for w in words if w in lower and lower[w].strip()}


def pending(db, scenes: bool = False) -> list[tuple[str, str, str, dict[str, str], list[str]]]:
    """Built clips (or German scenes) with words still missing a gloss: (id, title, english, missing words, lines)."""
    glossed: dict[str, set[str]] = {}
    for cid, infl in db.execute("select clip_id, inflection from glosses"):
        glossed.setdefault(cid, set()).add(infl)
    if scenes:
        items = [(sid, f"{show} · {title}", " ".join(e[2] for e in json.loads(en)))
                 for sid, show, title, en in db.execute(
                     "select s.id, e.show_title, e.title, s.english from scenes s join episodes e using (source)"
                     " where s.german = 1 order by s.source, s.idx")]
    else:
        items = db.execute("select id, title, english from clips where built = 1 order by id").fetchall()
    out = []
    for cid, title, english in items:
        lines, words = clip_words(db, cid)
        missing = {w: l for w, l in words.items() if w not in glossed.get(cid, ())}
        if missing:
            out.append((cid, title, english, missing, lines))
    return out


def known_words() -> set[str]:
    """The feed person's known words as dojo-feed counts them: what their kumapie sent
    (state\\feed\\known\\<person>.json) plus the words marked known on the TV."""
    f = STATE / "known" / f"{PERSON}.json"
    words = set(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else set()
    marks = cfg.state / "morphs" / f"{PERSON}-known-words.txt"
    if marks.exists():
        words |= {w.strip() for w in marks.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")}
    return words


def in_pool(db) -> set[str]:
    """Clips in today's feed (level 0 or 1 by [known_words], as the service counts)."""
    status = known_words()
    unknown: dict[str, set[str]] = {}
    for cid, toks in db.execute("select clip_id, tokens from cues"):
        unknown.setdefault(cid, set()).update(t[2] for t in json.loads(toks) if t[2] and t[2] not in status)
    return {cid for cid, u in unknown.items() if len(u) <= 1}


def cmd_export(args) -> None:
    """Write clips without glosses as JSON batches, for glossing outside the API (e.g. in a Claude session)."""
    db = connect()
    todo = pending(db, scenes=args.scenes)
    if args.pool:
        pool = in_pool(db)
        todo = [t for t in todo if t[0] in pool]
    args.dir.mkdir(parents=True, exist_ok=True)
    for n in range(0, len(todo), args.batch):
        batch = [{"id": cid, "show": re.sub(r" · .*", "", title), "lines": lines, "english": english,
                  "words": [w + (f" (from {l})" if " " in w else "") for w, l in missing.items()]}
                 for cid, title, english, missing, lines in todo[n:n + args.batch]]
        (args.dir / f"batch_{n // args.batch + 1:02d}.json").write_text(
            json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"{len(todo)} clips ({sum(len(t[3]) for t in todo)} words) in {args.dir}; "
        f"answer each batch as {{clip id: {{word: gloss}}}} and load it with `feed.py import`")


def cmd_import(args) -> None:
    """Load {clip id: {word: gloss}} JSON files written for `export` batches."""
    db = connect()
    total = 0
    for path in args.files:
        data = json.loads(path.read_text(encoding="utf-8"))
        for cid, glosses in data.items():
            _, words = clip_words(db, cid)
            rows = parse_reply(json.dumps(glosses, ensure_ascii=False), words)
            db.executemany("insert or replace into glosses values (?, ?, ?, ?, ?)",
                           [(cid, w, g, args.model, PROMPT.stem) for w, g in rows.items()])
            total += len(rows)
            missing = set(words) - set(rows)
            if missing and not db.execute("select 1 from glosses where clip_id = ? and inflection in (%s)"
                                          % ",".join("?" * len(missing)), (cid, *missing)).fetchone():
                log(f"{path.name}: {cid} has no gloss for {', '.join(sorted(missing))}")
    db.commit()
    log(f"imported {total} glosses")


def cmd_gloss(args) -> None:
    need("anthropic", cfg.python)
    import llm as common

    db = connect()
    todo = pending(db)
    words = sum(len(t[3]) for t in todo)
    model = args.model or F["gloss_model"]
    pin, pout = PRICE.get(model, (1.0, 5.0))
    est_in = sum(len(gloss_prompt(t[1], t[4], t[2], t[3])) / 3.5 for t in todo)
    est_out = words * 22
    log(f"{len(todo)} clips need glosses ({words} words); {model}: about "
        f"${(est_in * pin + est_out * pout) / 1e6:.2f} ({est_in / 1e6:.2f}M in, {est_out / 1e6:.2f}M out tokens)")
    if args.estimate or not todo:
        return
    if args.limit:
        random.seed(args.seed)
        todo = random.sample(todo, min(args.limit, len(todo)))

    client = common.make_client()
    mid = common.model_id(model)
    usage = [0, 0]
    shown = 0

    def one(item):
        cid, title, english, missing, lines = item
        got: dict[str, str] = {}
        for _ in range(2):  # a second try for words the first reply left out
            ask = {w: l for w, l in missing.items() if w not in got}
            r = common.call(client, model=mid, max_tokens=60 + 60 * len(ask),
                            messages=[{"role": "user", "content": gloss_prompt(title, lines, english, ask)}])
            usage[0] += r.usage.input_tokens
            usage[1] += r.usage.output_tokens
            got.update(parse_reply(common.text_of(r), ask))
            if len(got) == len(missing):
                break
        return item, got

    start = time.time()
    with ThreadPoolExecutor(4) as pool:
        for n, fut in enumerate(as_completed([pool.submit(one, t) for t in todo]), 1):
            try:
                (cid, title, english, missing, lines), got = fut.result()
            except (common.anthropic.AuthenticationError, common.anthropic.PermissionDeniedError) as e:
                pool.shutdown(cancel_futures=True)
                sys.exit(f"feed: the API key was refused ({e.message}) - renew it in .env")
            except Exception as e:
                log(f"gloss request failed: {e}")
                continue
            db.executemany("insert or replace into glosses values (?, ?, ?, ?, ?)",
                           [(cid, w, g, model, PROMPT.stem) for w, g in got.items()])
            db.commit()
            if args.limit and shown < args.limit:
                shown += 1
                print(f"\n[{cid}] {title}\n  " + "\n  ".join(lines) + f"\n  EN: {english}")
                for w in missing:
                    print(f"    {got.get(w, '!! no gloss: ' + w)}")
            if n % 50 == 0:
                log(f"  {n}/{len(todo)} clips, ${(usage[0] * pin + usage[1] * pout) / 1e6:.2f} so far")
    cost = (usage[0] * pin + usage[1] * pout) / 1e6
    log(f"done in {time.time() - start:.0f} s: {usage[0]} in / {usage[1]} out tokens = ${cost:.3f}"
        + (f" (${cost / len(todo):.4f} per clip)" if todo else ""))


# ---------------------------------------------------------------- one meaning per word (the TV app)
# The TV shows a word's own meaning (word_glosses), or a scene's meaning where the word means something
# else there (glosses, keyed by scene id like the feed's clip glosses). Decided 2026-10-04: one meaning per
# word instead of ~41,000 per scene; written in Claude sessions, not through the API.

def cmd_export_words(args) -> None:
    db = connect()
    have = {r[0] for r in db.execute("select inflection from word_glosses")}
    have |= {r[0] for r in db.execute("select distinct inflection from glosses")}
    words: dict[str, dict] = {}
    rows = db.execute(
        "select c.scene_id, c.text, c.tokens, s.english, e.show_title from scene_cues c"
        " join scenes s on s.id = c.scene_id join episodes e on e.source = s.source"
        " where s.german = 1 and s.source like ? order by s.source, s.idx, c.idx", (f"%{args.only or ''}%",))
    for sid, text, toks, english, show in rows:
        en = " ".join(e[2] for e in json.loads(english))
        for surface, _, infl, lemma in json.loads(toks):
            if not infl or infl in have:
                continue
            w = words.setdefault(infl, {"word": infl, "written": surface, "lemma": lemma, "show": show,
                                        "lines": []})
            if len(w["lines"]) < 3 and all(x["scene"] != sid for x in w["lines"]):
                w["lines"].append({"scene": sid, "line": text, "english": en})
    todo = list(words.values())
    args.dir.mkdir(parents=True, exist_ok=True)
    for n in range(0, len(todo), args.batch):
        (args.dir / f"words_{n // args.batch + 1:02d}.json").write_text(
            json.dumps(todo[n:n + args.batch], ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"{len(todo)} words without a meaning in {args.dir}; answer each batch as "
        '{"words": {word: meaning}, "scenes": {scene id: {word: meaning where it differs}}} and load it with '
        "`feed.py import-words`")


def cmd_import_words(args) -> None:
    db = connect()
    known = {r[0] for r in db.execute("select distinct inflection from glosses")}
    for (toks,) in db.execute("select tokens from scene_cues"):
        known.update(t[2] for t in json.loads(toks) if t[2])
    scenes = {r[0] for r in db.execute("select id from scenes")}
    n_words = n_scenes = 0
    for path in args.files:
        data = json.loads(path.read_text(encoding="utf-8"))
        for word, gloss in data.get("words", {}).items():
            if word not in known or not isinstance(gloss, str) or not gloss.strip():
                log(f"{path.name}: skipped word {word!r}")
                continue
            db.execute("insert or replace into word_glosses values (?, ?, ?, ?)", (word, gloss.strip(), args.model, "words_v1"))
            n_words += 1
        for sid, glosses in data.get("scenes", {}).items():
            if sid not in scenes:
                log(f"{path.name}: no scene {sid}")
                continue
            for word, gloss in glosses.items():
                db.execute("insert or replace into glosses values (?, ?, ?, ?, ?)", (sid, word, gloss.strip(), args.model, "words_v1"))
                n_scenes += 1
    db.commit()
    log(f"imported {n_words} word meanings and {n_scenes} scene meanings")


# ---------------------------------------------------------------- definitions v2: per line (the TV app)
# Every red/orange word of every line gets its own entry (decided 2026-10-04): dictionary form, English
# (translation + explanation), a German dictionary-style definition (split into words here, so the TV can
# colour them and let you pick them), and a grammar line when the word is a grammar point.

def cmd_export_defs(args) -> None:
    db = connect()
    status = known_words()
    done = {(a, b, c) for a, b, c in db.execute("select scene_id, cue_idx, inflection from line_defs")}
    scenes: dict[str, dict] = {}
    rows = db.execute(
        "select s.id, s.source, s.idx, s.english, e.show_title, e.title, c.idx, c.text, c.tokens"
        " from scenes s join episodes e using (source) join scene_cues c on c.scene_id = s.id"
        " where s.german = 1 and s.source like ? order by s.source, s.idx, c.idx", (f"%{args.only or ''}%",))
    for sid, source, sidx, english, show, title, cidx, text, toks in rows:
        sc = scenes.setdefault(sid, {"scene": sid, "where": f"{show} {title}, scene {sidx + 1}",
                                     "lines": [], "english": " ".join(e[2] for e in json.loads(english)),
                                     "words": []})
        sc["lines"].append(text)
        seen = set()
        for surface, _, infl, lemma in json.loads(toks):
            if not infl or infl in seen or infl in status or (sid, cidx, infl) in done:
                continue
            seen.add(infl)
            sc["words"].append({"line": cidx, "word": infl, "written": surface, "lemma_hint": lemma,
                                "status": "learning" if infl in status else "new"})
    todo = [sc for sc in scenes.values() if sc["words"]]
    args.dir.mkdir(parents=True, exist_ok=True)
    batch, n, files = [], 0, 0
    for sc in todo:
        batch.append(sc)
        n += len(sc["words"])
        if n >= args.batch:
            files += 1
            (args.dir / f"defs_{files:02d}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
            batch, n = [], 0
    if batch:
        files += 1
        (args.dir / f"defs_{files:02d}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"{sum(len(sc['words']) for sc in todo)} words in {len(todo)} scenes, {files} files in {args.dir}; answer "
        'as {"<scene>": {"<line>|<word>": {"lemma", "en", "de", "gr"}}} and load with `feed.py import-defs`')


def cmd_import_defs(args) -> None:
    need(cfg.raw["morphs"]["spacy_model"], cfg.python)  # the German definitions are split into words
    db = connect()
    parse, _ = make_parser()
    entries = []
    for path in args.files:
        for sid, words in json.loads(path.read_text(encoding="utf-8")).items():
            for key, d in words.items():
                line, word = key.split("|", 1)
                entries.append((sid, int(line), word, d))
    known_keys = {(a, b) for a, b in db.execute("select scene_id, idx from scene_cues")}
    bad = [e for e in entries if (e[0], e[1]) not in known_keys]
    for e in bad:
        log(f"no line {e[1]} in scene {e[0]} ({e[2]})")
    entries = [e for e in entries if (e[0], e[1]) in known_keys]
    parsed = parse([e[3].get("de", "") for e in entries])
    for (sid, line, word, d), toks in zip(entries, parsed):
        db.execute("insert or replace into line_defs values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (sid, line, word, d.get("lemma", ""), d.get("en", ""), d.get("de", ""), d.get("gr", ""),
                    json.dumps(toks, ensure_ascii=False), args.model))
    db.commit()
    log(f"imported {len(entries)} line definitions" + (f", skipped {len(bad)}" if bad else ""))


# ---------------------------------------------------------------- show

def cmd_show(args) -> None:
    db = connect()
    cid = args.id or (db.execute("select id from clips order by random() limit 1").fetchone() or [None])[0]
    row = db.execute("select title, source, start, end, english, built from clips where id = ?", (cid,)).fetchone()
    if not row:
        sys.exit(f"feed: no clip {cid}")
    title, source, start, end, english, built = row
    print(f"{cid}  {title}  {source} {start:.2f}-{end:.2f} ({end - start:.1f} s){'' if built else '  NOT CUT'}")
    glosses = dict(db.execute("select inflection, gloss from glosses where clip_id = ?", (cid,)))
    for s, e, text, toks in db.execute("select start, end, text, tokens from cues where clip_id = ? order by idx",
                                       (cid,)):
        print(f"  {s:5.2f}-{e:5.2f}  {text}")
        for surface, at, infl, lemma in json.loads(toks):
            if infl:
                print(f"      {surface:<14} {infl:<16} {glosses.get(infl, '')}")
    print(f"  EN: {english}\n  {CLIPS / (cid + '.mp4')}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="feed", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build", help="cut clips and index them")
    p.add_argument("--only", metavar="TEXT", help="only videos whose path contains TEXT (e.g. s1_01)")
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("scenes", help="split whole episodes into scenes for the TV app (no cutting)")
    p.add_argument("--only", metavar="TEXT", help="only videos whose path contains TEXT (e.g. s1_01)")
    p.add_argument("--print", action="store_true", help="print the scenes of those videos")
    p.set_defaults(func=cmd_scenes)
    p = sub.add_parser("gloss", help="write word glosses with Claude (paid)")
    p.add_argument("--estimate", action="store_true", help="only count and estimate the cost")
    p.add_argument("--limit", type=int, help="gloss only this many random clips and print them")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--model", help=f"default: {F['gloss_model']}")
    p.set_defaults(func=cmd_gloss)
    p = sub.add_parser("export", help="write clips without glosses as JSON batches (gloss without the API)")
    p.add_argument("dir", type=Path)
    p.add_argument("--pool", action="store_true", help="only clips in today's feed (i+0/i+1)")
    p.add_argument("--scenes", action="store_true", help="the TV app's scenes instead of the feed clips")
    p.add_argument("--batch", type=int, default=25, help="clips per file (default 25)")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("import", help="load glosses written for export batches")
    p.add_argument("files", type=Path, nargs="+")
    p.add_argument("--model", default="claude-session", help="stored as the gloss's source")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("export-defs", help="per-line definitions still to write (definitions v2)")
    p.add_argument("dir", type=Path)
    p.add_argument("--only", metavar="TEXT", help="only videos whose path contains TEXT (e.g. s1_01)")
    p.add_argument("--batch", type=int, default=100, help="about this many words per file (whole scenes)")
    p.set_defaults(func=cmd_export_defs)
    p = sub.add_parser("import-defs", help="load per-line definitions")
    p.add_argument("files", type=Path, nargs="+")
    p.add_argument("--model", default="claude-session", help="stored as the definition's source")
    p.set_defaults(func=cmd_import_defs)
    p = sub.add_parser("export-words", help="the TV's words with no meaning yet, as JSON batches")
    p.add_argument("dir", type=Path)
    p.add_argument("--only", metavar="TEXT", help="only videos whose path contains TEXT (e.g. s1_01)")
    p.add_argument("--batch", type=int, default=100, help="words per file (default 100)")
    p.set_defaults(func=cmd_export_words)
    p = sub.add_parser("import-words", help="load word meanings written for export-words batches")
    p.add_argument("files", type=Path, nargs="+")
    p.add_argument("--model", default="claude-session", help="stored as the meaning's source")
    p.set_defaults(func=cmd_import_words)
    p = sub.add_parser("show", help="print a clip")
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_show)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
