"""Lyrics for the openings and endings of Japanese anime in the library: sung lines ("♪ ...", jimaku.SONG) written
into <stem>.srt in every stretch of 45 s+ without said lines, then the episode is rebuilt (pc_episodes.py --force).

Where the words come from, per show (kept in state\\feed\\lyrics\\<show>.json):
  1. Jimaku's subtitle files for the episode that carry the song lines (fan-made .ass: styles OP, ED-JP...);
  2. LRCLIB's timed lyrics of the show's theme songs (their names from AnimeThemes, else MyAnimeList through Jikan).
Where they go: Parakeet listens to the stretch, and each line it hears that shares 3+ letters in a row with a lyric
line votes for (heard time - lyric time). 3+ heard lines agreeing within 1.5 s place the song; a TV cut that jumps in
the full song gets a second placement after the jump. Nothing agrees: no lyrics, the stretch stays as it was.
(alass can't do this: it takes instruments for speech, and put Apothecary Diaries 03's songs 9 and 21 s off.)

  .venv\\Scripts\\python.exe tools\\kumapie\\lyrics.py --for pedro-ja [<.mp4 files>]     (default: every anime episode)
  .venv\\Scripts\\python.exe tools\\kumapie\\lyrics.py test                               (the placing rules)
  ... --for pedro-ja --song "<title>|<artist>" <.mp4 files>     (a theme song named by hand, when no service answers)
"""
from __future__ import annotations

import argparse
import difflib
import functools
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

CODE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE))
from dojo import cfg  # noqa: E402
sys.path.insert(0, str(cfg.kumapie_app / "mobile" / "src" / "main" / "python"))
import jimaku  # noqa: E402

CONF = cfg.raw["aniworld"]
PARAKEET = [cfg.root / CONF["venv"] / "Scripts" / "python.exe", cfg.pipeline / "parakeet.py", "--ja", cfg.root / CONF["parakeet_ja"]]
POOLS = cfg.root / "state" / "feed" / "lyrics"
GAP = 45.0     # kumapie's SceneController.SKIP_GAP: a stretch this long without said lines is an opening or ending
CLEAR = 0.35   # lyrics stay this far from said lines: over newepisode.PAD, so the scenes beside keep their ids
AGREE = 1.5    # votes this close are one placement
EPISODE = re.compile(r"_s(\d+)_(\d+)$")
STAMP = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{3}) --> (\d+):(\d\d):(\d\d)[,.](\d{3})")


def _get(url: str, data: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": jimaku.AGENT, "Content-Type": "application/json"},
                                 data=json.dumps(data).encode() if data else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- the .srt, kept to the millisecond

def read_srt(path: Path) -> list[list]:
    """[[start, end, text]]; not feed.read_srt, which shortens long lines (a changed time is a new scene id)."""
    out = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")):
        m = STAMP.search(block)
        text = " ".join(x.strip() for x in block[m.end():].split("\n") if x.strip()) if m else ""
        if text:
            t = [int(x) for x in m.groups()]
            out.append([t[0] * 3600 + t[1] * 60 + t[2] + t[3] / 1000, t[4] * 3600 + t[5] * 60 + t[6] + t[7] / 1000, text])
    return out


def write_srt(cues: list, path: Path) -> None:
    def ts(t):
        ms = round(t * 1000)
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    path.write_text("".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(cues, 1)), encoding="utf-8")


def gaps(cues: list, duration: float) -> list[tuple[float, float]]:
    """Stretches of [GAP]+ without said lines that have no lyrics yet, each [CLEAR] inside its said lines."""
    said = sorted(c for c in cues if not c[2].startswith("♪"))
    edges = [0.0] + [t for c in said for t in (c[0], c[1])] + [duration]
    out = []
    for a, b in zip(edges[::2], edges[1::2]):
        if b - a >= GAP and sum(1 for c in cues if c[2].startswith("♪") and a <= c[0] < b) < 3:
            out.append((a + CLEAR if a else 0.0, b - CLEAR))
    return out


# ---------------------------------------------------------------- placing

def _letters(text: str) -> str:
    return re.sub(r"[^぀-ヿ一-鿿]", "", text)


def place(heard: list, lines: list, a: float, b: float) -> tuple[int, list]:
    """[heard] [[time, text]] (Parakeet, video time) and a song's [lines] [[start, end, text]] (the song's own time)
    -> (heard lines that agree, the lines in video time inside a..b)."""
    votes = []  # (offset, heard time, both start with the shared letters: the offset is exact)
    for t, text in heard:
        h = _letters(text)
        for la, _, lx in lines:
            x = _letters(lx)
            m = difflib.SequenceMatcher(None, h, x, autojunk=False).find_longest_match(0, len(h), 0, len(x))
            if m.size >= 3:
                votes.append((t - la, t, m.a + m.b <= 1))
    found = []  # (offset, first heard, last heard, how many heard lines)
    while votes:
        best = max(votes, key=lambda v: (len({t for d, t, _ in votes if abs(d - v[0]) <= AGREE}), v[0]))
        near = [v for v in votes if abs(v[0] - best[0]) <= AGREE]
        times = sorted({t for _, t, _ in near})
        if len(times) < 3:
            break
        # A chorus heard once votes for each time the song has it: the placement taken first (most lines, then the
        # earliest in the song) keeps those heard lines.
        if not any(lo <= t <= hi for _, lo, hi, _ in found for t in times):
            exact = [d for d, _, e in near if e]
            found.append((statistics.median(exact if len(exact) >= 2 else [d for d, _, _ in near]), times[0], times[-1], len(times)))
        votes = [v for v in votes if abs(v[0] - best[0]) > AGREE]
    found.sort(key=lambda f: f[1])
    out = []
    for i, (off, lo, hi, _) in enumerate(found):
        # Each placement rules from half way after the one before to half way before the next (a TV cut's jump).
        r0 = a if i == 0 else (found[i - 1][2] + lo) / 2
        r1 = b if i + 1 == len(found) else (hi + found[i + 1][1]) / 2
        for la, lb, text in lines:
            s = la + off
            if max(a, r0) <= s < min(b, r1) - 0.5:
                out.append([round(s, 3), round(min(lb + off, b, r1), 3), jimaku.SONG + text.removeprefix(jimaku.SONG)])
    return sum(f[3] for f in found), out


def heard(video: Path, a: float, b: float, tmp: Path) -> list:
    """What Parakeet hears in a..b of [video]: [[time in the video, text]]."""
    ffmpeg = os.environ.get("FFMPEG") or "ffmpeg"
    clip = tmp / "clip.wav"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", str(a), "-to", str(b), "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(clip)], check=True)
    (tmp / "heard.srt").unlink(missing_ok=True)
    subprocess.run([*map(str, PARAKEET), "-o", "heard", str(clip)], cwd=tmp, check=True, capture_output=True,
                   env={**os.environ, "PYTHONUTF8": "1", "HF_HOME": str(cfg.root / CONF["hf_home"])})
    out = tmp / "heard.srt"
    return [[a + c[0], c[2]] for c in read_srt(out)] if out.exists() else []


# ---------------------------------------------------------------- the words

def sets_of(cues: list) -> list[list]:
    """A subtitle file's sung lines -> its songs (a break of 20 s+ starts the next), 4+ lines each."""
    songs: list[list] = []
    for c in (c for c in cues if c[2].startswith("♪")):
        if songs and c[0] - songs[-1][-1][1] <= 20:
            songs[-1].append(c)
        else:
            songs.append([c])
    return [s for s in songs if len(s) >= 4]


def from_jimaku(key: str, files: list, number, tmp: Path) -> list[dict]:
    """The song lines in Jimaku's files for this episode: styled subtitles first (they are the ones with songs), 3 files at most."""
    out = []
    for f in sorted(files, key=lambda f: not f["name"].lower().endswith((".ass", ".ssa")))[:3]:
        try:
            path = jimaku.download(key, f["url"], f["name"], number, str(tmp / "jimaku"))
            for s in sets_of(json.loads(jimaku.cues(path, "ja"))):
                out.append({"from": f"jimaku: {f['name']}", "lines": s})
        except Exception as e:  # noqa: BLE001 - a zip without the episode, a failed download: the next file
            print(f"  jimaku {f['name']}: {e}", flush=True)
    return out


@functools.cache  # once a run: a service that is down is not asked again for every song
def themes(anilist: int) -> list[tuple[str, str]] | None:
    """The show's opening and ending songs as (title, artist), a title in each spelling known; None when no service answers."""
    try:
        q = urllib.parse.quote
        found = _get(f"https://api.animethemes.moe/anime?{q('filter[has]')}=resources&{q('filter[site]')}=AniList"
                     f"&{q('filter[external_id]')}={anilist}&include=animethemes.song.artists")["anime"]
        return [(t["song"]["title"], (t["song"]["artists"] or [{}])[0].get("name", ""))
                for a in found for t in a["animethemes"] if t.get("song")]
    except Exception as e:  # noqa: BLE001
        print(f"  animethemes: {e}", flush=True)
    try:
        mal = _get("https://graphql.anilist.co", {"query": "query($i:Int){Media(id:$i){idMal}}", "variables": {"i": anilist}})["data"]["Media"]["idMal"]
        data = _get(f"https://api.jikan.moe/v4/anime/{mal}/themes")["data"]
        out = []
        for line in data["openings"] + data["endings"]:  # 1: "Hana ni Natte (花になって)" by Ryokuoushoku Shakai (eps 1-12)
            m = re.search(r'"(.+?)(?: \(([^)]+)\))?"\s+by\s+(.+?)(?:\s+\(ep.*)?$', line)
            if m:
                out += [(t, m[3]) for t in (m[2], m[1]) if t]
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  jikan: {e}", flush=True)
    return None


def from_lrclib(songs: list[tuple[str, str]]) -> list[dict]:
    """LRCLIB's timed lyrics of each song, up to 3 recordings (a "TV size" one among them fits best: the vote picks)."""
    out, seen = [], set()
    for title, artist in songs:
        try:
            hits = _get("https://lrclib.net/api/search?" + urllib.parse.urlencode({"track_name": title, "artist_name": artist})) \
                or _get("https://lrclib.net/api/search?" + urllib.parse.urlencode({"track_name": title}))
        except Exception as e:  # noqa: BLE001
            print(f"  lrclib {title}: {e}", flush=True)
            continue
        for hit in [h for h in hits if h.get("syncedLyrics")][:3]:
            at = [(int(m[1]) * 60 + float(m[2]), m[3].strip()) for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", hit["syncedLyrics"])]
            at = [(t, x) for t, x in at if re.search(r"[ぁ-ヿ]", x)]  # Japanese lines (not a romaji or translated copy)
            lines = [[t, min(t + 8, at[i + 1][0] if i + 1 < len(at) else t + 8), x] for i, (t, x) in enumerate(at)]
            if len(lines) >= 4 and (hit["id"] not in seen):
                seen.add(hit["id"])
                out.append({"from": f"lrclib: {hit['trackName']} / {hit['artistName']} ({hit['duration']:.0f} s)", "lines": lines})
    return out


# ---------------------------------------------------------------- an episode

def _pool(show: str) -> dict:
    store = POOLS / f"{show}.json"
    return json.loads(store.read_text(encoding="utf-8")) if store.exists() else {"sets": [], "jimaku": [], "songs": False}


def _keep(show: str, pool: dict) -> None:
    POOLS.mkdir(parents=True, exist_ok=True)
    (POOLS / f"{show}.json").write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")


def add(video: Path, key: str) -> bool:
    """Lyrics into [video]'s .srt where a song has none; True when the file changed."""
    srt = video.with_suffix(".srt")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    cues = read_srt(srt)
    todo = gaps(cues, float(probe.stdout.strip() or 0))
    if not todo:
        return False
    show = video.parent.name
    m = EPISODE.search(video.stem)
    season, number = (int(m[1]), int(m[2])) if m else (None, None)
    pool = _pool(show)
    changed = False
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for a, b in todo:
            said = heard(video, a, b, tmp)

            def best():
                got = [(place(said, s["lines"], a, b), s["from"]) for s in pool["sets"]]
                return max(got, key=lambda g: g[0][0], default=((0, []), ""))
            (n, lines), source = best()
            if n == 0 and key and video.stem not in pool["jimaku"]:  # this episode's files, asked once
                found = json.loads(jimaku.candidates(key, show.replace("_", " "), season, number))
                pool["anilist"] = (found.get("anilist") or {}).get("id") or pool.get("anilist")
                pool["sets"] += from_jimaku(key, found["files"], number, tmp)
                pool["jimaku"].append(video.stem)
                (n, lines), source = best()
            if n == 0 and not pool["songs"] and pool.get("anilist"):  # the show's songs, asked once that works
                songs = themes(pool["anilist"])
                if songs is not None:
                    pool["sets"] += from_lrclib(songs)
                    pool["songs"] = True
                    (n, lines), source = best()
            print(f"  {a:7.1f}-{b:7.1f}: {len(said)} lines heard, {n} agree, {len(lines)} lyric lines ({source or 'nothing fits'})", flush=True)
            if lines:
                cues += lines
                changed = True
    _keep(show, pool)
    if changed:
        write_srt(sorted(cues, key=lambda c: c[0]), srt)  # by start only: lines starting together keep their order (the scene's end, so its id)
    return changed


def test() -> None:
    song = [[10 + 5 * i, 14 + 5 * i, t] for i, t in enumerate(["あいうえおかきく", "さしすせそたちつ", "なにぬねのはひふ", "まみむめもやゆよ", "らりるれろわをん", "がぎぐげござじず"])]
    # The song's 10 s plays at 100 s; a TV cut then jumps: the lines from the song's 25 s play again from 150 s.
    said = [[100.2, "あいうえお"], [105.1, "さしすせそ"], [110.0, "なにぬねの"], [150.0, "まみむめも"], [155.0, "らりるれろ"], [160.0, "がぎぐげご"]]
    n, lines = place(said[:3], song, 95, 200)
    assert n == 3 and [round(l[0]) for l in lines] == [100, 105, 110, 115, 120, 125], lines
    assert place(said[:2], song, 95, 200) == (0, [])  # two lines agreeing place nothing
    assert place(said[:3], song, 95, 112)[1][-1][1] == 112, "kept inside the stretch"
    n, lines = place(said, song, 95, 200)
    assert n == 6 and [round(l[0]) for l in lines] == [100, 105, 110, 115, 120, 125, 135, 140, 145, 150, 155, 160], lines  # the jump: half way, 130 s
    cues = [[1.0, 2.0, "a"], [100.0, 101.0, "b"], [102.0, 103.0, "c"]]
    assert gaps(cues, 150.0) == [(2.0 + CLEAR, 100.0 - CLEAR), (103.0 + CLEAR, 150.0 - CLEAR)], gaps(cues, 150.0)
    assert gaps(cues + [[10.0 * i, 10.0 * i + 5, "♪ x"] for i in range(1, 4)], 120.0) == []
    with tempfile.TemporaryDirectory() as d:
        write_srt([[1.0, 2.5, "あ"], [3723.004, 3724.0, "♪ い"]], Path(d) / "a.srt")
        assert read_srt(Path(d) / "a.srt") == [[1.0, 2.5, "あ"], [3723.004, 3724.0, "♪ い"]]
    assert len(sets_of([[i * 5.0, i * 5.0 + 4, "♪ x"] for i in range(5)] + [[200.0, 201.0, "♪ y"]])) == 1
    print("ok")


def main() -> None:
    if sys.argv[1:] == ["test"]:
        return test()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--for", dest="person", required=True, help="library key (pedro-ja)")
    ap.add_argument("videos", nargs="*", type=Path, help="only these .mp4 files (default: every anime episode)")
    ap.add_argument("--song", action="append", default=[], metavar="TITLE|ARTIST",
                    help="a theme song of the named videos' show, when no service knows the show's songs (LRCLIB is asked for it)")
    a = ap.parse_args()
    key = os.environ.get("JIMAKU_API_KEY", "")
    for show in {v.parent.name for v in a.videos} if a.song else ():
        pool = _pool(show)
        got = from_lrclib([tuple((s + "|").split("|")[:2]) for s in a.song])
        pool["sets"] += [s for s in got if s["from"] not in {p["from"] for p in pool["sets"]}]
        _keep(show, pool)
        print(f"{show}: {len(got)} recording(s) with timed lyrics on LRCLIB", flush=True)
    done = []
    for video in sorted(a.videos or (cfg.library(a.person) / "anime").rglob("*.mp4")):
        made = video.with_name(video.stem + ".kumapie.json")
        if not video.with_suffix(".srt").exists():
            continue
        if made.exists() and "source" not in json.loads(made.read_text(encoding="utf-8")):
            print(f"{video.stem}: made on a device, left alone (pc_episodes.py would give it a new id)", flush=True)
            continue
        print(video.stem, flush=True)
        try:
            if add(video, key):  # the episode again from its .srt, at once: a run that stops leaves none half done
                subprocess.run([cfg.python, CODE / "tools" / "kumapie" / "pc_episodes.py", "--for", a.person, "--force", video], check=True)
                done.append(video)
        except Exception as e:  # noqa: BLE001 - one episode's trouble (a download, Parakeet) doesn't stop the others
            print(f"  failed: {e}", flush=True)
    print(f"lyrics added to {len(done)} episode(s)", flush=True)


if __name__ == "__main__":
    main()
