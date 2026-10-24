"""Known words per person and language, as each person's kumapie says (kumapie_plan.md item 6).

The phone sends the words it draws white or green (its own Mined card studied, marked known, or an imported list
such as Jiten's for Japanese) with POST /api/tv/known_words {"person": sync person, "lang": "de", "words": [...]}.
They're kept in state\\feed\\known\\<library key>.json (a sorted list) and read by the feed, the TV and
tools\\feed\\feed.py. No file yet = nothing known.
"""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # code/
from dojo import cfg  # noqa: E402

PEOPLE: dict[str, dict[str, str]] = cfg.raw.get("sync", {}).get("people", {})
# A device without an owner set is the feed person's (as the TV API treats it).
OWNER = next((p for p, langs in PEOPLE.items() if cfg.raw["feed"]["person"] in langs.values()), None)
DIR = cfg.state / "feed" / "known"
MAX_WORDS = 200_000
MAX_LEN = 64

_lock = threading.Lock()
_cache: dict[str, tuple[float, frozenset[str]]] = {}


def path(key: str) -> Path:
    return DIR / f"{key}.json"


def save(body: dict) -> dict:
    """Stores a phone's list. ValueError when the person, language or words aren't right."""
    person, lang, words = body.get("person") or OWNER, body.get("lang"), body.get("words")
    key = PEOPLE.get(person, {}).get(lang) if isinstance(person, str) and isinstance(lang, str) else None
    if not key:
        raise ValueError("unknown person or language")
    if not isinstance(words, list) or len(words) > MAX_WORDS or not all(
            isinstance(w, str) and w.strip() and len(w) <= MAX_LEN and "\n" not in w for w in words):
        raise ValueError("words must be a list of short strings")
    out = sorted({w.strip() for w in words})
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = path(key).with_suffix(".part")
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path(key))
    return {"ok": True, "key": key, "n": len(out)}


def listing(person: str | None, lang: str | None) -> list[str]:
    """The person's known words in a language as their phone sent them (kumadeck on the Steam Deck reads them)."""
    key = PEOPLE.get(person or OWNER, {}).get(lang) if isinstance(lang, str) else None
    if not key:
        raise ValueError("unknown person or language")
    return sorted(words(key) or ())


def words(key: str) -> frozenset[str] | None:
    """The person's known words (reread when the file changes), or None when their phone never sent any."""
    f = path(key)
    try:
        m = f.stat().st_mtime
    except OSError:
        return None
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == m:
            return hit[1]
        try:
            ws = frozenset(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return hit[1] if hit else None
        _cache[key] = (m, ws)
        return ws


if __name__ == "__main__":  # self-check in a temporary folder
    import tempfile
    DIR = Path(tempfile.mkdtemp())
    PEOPLE = {"pedro": {"de": "pedro-de"}}
    assert words("pedro-de") is None
    assert save({"person": "pedro", "lang": "de", "words": ["haus", "Ich ", "haus"]}) == {"ok": True, "key": "pedro-de", "n": 2}
    assert words("pedro-de") == {"haus", "Ich"}
    for bad in ({"person": "x", "lang": "de", "words": []}, {"person": "pedro", "lang": "ja", "words": []},
                {"person": "pedro", "lang": "de", "words": "haus"}, {"person": "pedro", "lang": "de", "words": ["a\nb"]},
                {"person": "pedro", "lang": "de", "words": [1]}, {"person": "pedro", "lang": "de", "words": ["x" * 65]}):
        try:
            save(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    print("ok")
