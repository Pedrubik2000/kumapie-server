"""Built-in word SRS for people who don't use kuma3 (kumapie TV, config.toml [srs] people = [library keys]).

Cards live in state\\feed\\srs\\<library key>.json: {"cards": {word: card}, "known": [words marked known],
"undo": {"word", "before": card before the last rating} | null}. The scheduler is py-fsrs with default settings.
Card state "c": b = new (blue), d = due (today's study day), k = not due yet. The TV endpoints are in tv.py's docstring.
"""
import datetime
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fsrs import Card, Rating, Scheduler, State

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root
from dojo import cfg  # noqa: E402

DIR = cfg.state / "feed" / "srs"
PEOPLE: list[str] = list(cfg.raw.get("srs", {}).get("people", []))
MAX_LEN = 64
PAD = 0.25
_lock = threading.RLock()
_sched = Scheduler()


def enabled(key: str) -> bool:
    return key in PEOPLE


def _path(key: str) -> Path:
    return DIR / f"{key}.json"


def _load(key: str) -> dict:
    try:
        d = json.loads(_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {"cards": d.get("cards", {}), "known": d.get("known", []), "undo": d.get("undo")}


def _save(key: str, d: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(key).with_suffix(".part")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_path(key))


def _day_end(now: datetime.datetime) -> datetime.datetime:
    """The end of the study day containing now (days start at 04:00 local), timezone-aware."""
    local = now.astimezone()
    end = local.replace(hour=4, minute=0, second=0, microsecond=0)
    return end if end > local else end + datetime.timedelta(days=1)


def _day(epoch: float) -> str:
    return (datetime.datetime.fromtimestamp(epoch) - datetime.timedelta(hours=4)).date().isoformat()


def state_of(card: dict, now: datetime.datetime | None = None) -> str:
    """b / d / k for a stored card."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not card["reps"]:
        return "b"
    c = Card.from_dict(card["fsrs"])
    if c.state == State.Review:
        return "d" if c.due < _day_end(now) else "k"
    return "d" if c.due <= now else "k"


def known(key: str) -> frozenset[str]:
    """Words that draw as known: marked + cards that are k or d."""
    with _lock:
        d = _load(key)
    return frozenset(d["known"]) | {w for w, c in d["cards"].items() if state_of(c) in "kd"}


def words_info(key: str) -> tuple[dict, set]:
    """({word: card state}, marked set), read once for a whole episode."""
    with _lock:
        d = _load(key)
    return {w: state_of(c) for w, c in d["cards"].items()}, set(d["known"])


def _word(word) -> str:
    if not isinstance(word, str) or not word.strip() or len(word) > MAX_LEN or "\n" in word:
        raise ValueError("bad word")
    return word


def _text(v, n=500) -> str:
    return str(v or "").replace("\r", " ").replace("\n", " ")[:n]


def set_known(key: str, word: str, on: bool) -> dict:
    _word(word)
    with _lock:
        d = _load(key)
        ks = set(d["known"])
        ks = ks | {word} if on else ks - {word}
        d["known"] = sorted(ks)
        _save(key, d)
        card = d["cards"].get(word)
    return {"s": "k" if word in ks or (card and state_of(card) in "kd") else "u"}


def add(key: str, ep: dict, scene: str, line: int, word: str, meaning: str) -> dict:
    """Mines a word from a scene's line. ValueError when the scene, line or word don't fit."""
    _word(word)
    sc = next((s for s in ep["scenes"] if s["id"] == scene), None)
    if not sc or not isinstance(line, int) or isinstance(line, bool) or not 0 <= line < len(sc["cues"]):
        raise ValueError("unknown scene or line")
    if word not in ep["infl"]:
        raise ValueError("word not in this episode")
    cue = sc["cues"][line]
    sentence = cue.get("text") or "".join(seg[0] for seg in cue["seg"])
    tr = " ".join(e[2] for e in sc["english"] if len(e) > 2 and e[0] < cue["end"] and e[1] > cue["start"]) \
        or " ".join(e[2] for e in sc["english"] if len(e) > 2)
    with _lock:
        d = _load(key)
        if word in d["cards"]:
            return {"c": state_of(d["cards"][word])}
        d["cards"][word] = {
            "word": word, "added": int(time.time()), "episode": ep["id"], "scene": scene, "line": line,
            "sentence": _text(sentence), "translation": _text(tr), "meaning": _text(meaning),
            "start": cue["start"], "end": cue["end"], "fsrs": Card().to_dict(), "reps": 0, "lapses": 0, "log": []}
        _save(key, d)
    return {"c": "b"}


def _today(d: dict) -> int:
    today = _day(time.time())
    return sum(1 for c in d["cards"].values() for t, _ in c["log"] if _day(t) == today)


def rate(key: str, word: str, rating: int, now: datetime.datetime | None = None) -> dict:
    if isinstance(rating, bool) or rating not in (1, 2, 3, 4):
        raise ValueError("rating must be 1..4")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    with _lock:
        d = _load(key)
        card = d["cards"].get(word)
        if not card:
            raise ValueError("no such card")
        d["undo"] = {"word": word, "before": json.loads(json.dumps(card))}
        new, _log = _sched.review_card(Card.from_dict(card["fsrs"]), Rating(rating), now)
        if rating == 1 and card["reps"]:
            card["lapses"] += 1
        card.update(fsrs=new.to_dict(), reps=card["reps"] + 1)
        card["log"].append([int(now.timestamp()), rating])
        _save(key, d)
        return {"c": state_of(card, now), "today": _today(d)}


def undo(key: str) -> dict:
    with _lock:
        d = _load(key)
        u = d["undo"]
        if not u:
            raise ValueError("nothing to undo")
        d["cards"][u["word"]] = u["before"]
        d["undo"] = None
        _save(key, d)
        return {"word": u["word"], "c": state_of(u["before"])}


def due(key: str, lang: str, person: str) -> dict:
    with _lock:
        d = _load(key)
    rows = []
    for w, c in d["cards"].items():
        f = Card.from_dict(c["fsrs"])
        rows.append((state_of(c), f.state == State.Review, f.due.timestamp(), c["added"], w, c))
    # due ones: learning/relearning first, then reviews, by due; then the new ones by when they were mined
    # Anki's learn-ahead limit (20 min): a learning step coming up soon is shown now, not "all done" then back later.
    ahead = datetime.datetime.now(datetime.timezone.utc).timestamp() + 20 * 60
    ordered = sorted((r for r in rows if r[0] == "d" or (r[0] == "k" and not r[1] and r[2] <= ahead)), key=lambda r: (r[1], r[2])) \
        + sorted((r for r in rows if r[0] == "b"), key=lambda r: r[3])
    cards = [{"word": w, "sentence": c["sentence"], "translation": c["translation"], "meaning": c["meaning"], "c": s,
              "audio": f"/api/tv/srs/audio?person={quote(person)}&lang={quote(lang)}&word={quote(w)}"}
             for s, _, _, _, w, c in ordered]
    return {"due": sum(1 for r in rows if r[0] == "d"), "new": sum(1 for r in rows if r[0] == "b"),
            "today": _today(d), "cards": cards}


def audio(key: str, word: str, video: Path | None, ffmpeg: str) -> Path | None:
    """The card's line cut from the video (mp3, cached); None when there's no card or video."""
    with _lock:
        c = _load(key)["cards"].get(word)
    if not c or not video:
        return None
    out = DIR / "audio" / key / (hashlib.sha1(f"{c['episode']}|{c['start']}|{c['end']}".encode()).hexdigest() + ".mp3")
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp3")
    start = max(0.0, c["start"] - PAD)
    r = subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{c['end'] - start + PAD:.3f}", "-i", str(video),
                        "-vn", "-ac", "1", "-b:a", "64k", str(tmp)], capture_output=True, timeout=60)
    if r.returncode or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(out)
    return out


if __name__ == "__main__":  # self-check in a temporary folder
    import tempfile
    DIR = Path(tempfile.mkdtemp())
    ep = {"id": "e1", "infl": {"know", "this"}, "scenes": [{"id": "s1", "english": [[0, 5, "Lo sabemos"]], "cues": [
        {"start": 1.0, "end": 3.0, "seg": [["We", "we"], [" ", None], ["know", "know"]]}]}]}
    assert add("k", ep, "s1", 0, "know", "saber") == {"c": "b"}
    assert add("k", ep, "s1", 0, "know", "x") == {"c": "b"}
    c = _load("k")["cards"]["know"]
    assert c["sentence"] == "We know" and c["translation"] == "Lo sabemos" and c["meaning"] == "saber"
    for bad in (("s1", 1, "know"), ("s2", 0, "know"), ("s1", 0, "nope"), ("s1", 0, "a\nb")):
        try:
            add("k", ep, bad[0], bad[1], bad[2], "")
            raise AssertionError(bad)
        except ValueError:
            pass
    assert "know" not in known("k")
    assert rate("k", "know", 3)["c"] == "k" and "know" in known("k")
    d = _load("k")
    f = Card.from_dict(d["cards"]["know"]["fsrs"])
    f.due -= datetime.timedelta(hours=1)
    d["cards"]["know"]["fsrs"] = f.to_dict()
    _save("k", d)
    assert state_of(d["cards"]["know"]) == "d" and due("k", "en", "jackson")["due"] == 1
    assert undo("k") == {"word": "know", "c": "b"} and _load("k")["cards"]["know"]["reps"] == 0
    for fn in (lambda: undo("k"), lambda: rate("k", "zzz", 3), lambda: rate("k", "know", 5)):
        try:
            fn()
            raise AssertionError
        except ValueError:
            pass
    assert set_known("k", "this", True) == {"s": "k"} and words_info("k")[1] == {"this"}
    assert set_known("k", "this", False) == {"s": "u"} and words_info("k")[1] == set()
    print("ok")
