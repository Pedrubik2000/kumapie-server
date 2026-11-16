"""Known words per person and language, as each person's kumapie says (kumapie_plan.md item 6).

The phone sends the words it draws white or green (its own Mined card studied, marked known, or an imported list
such as Jiten's for Japanese) with POST /api/tv/known_words {"person": sync person, "lang": "de", "words": [...]}.
Each device's list ("device": its model) is kept in state\\feed\\known\\<library key>.devices\\<device>.json and their
union in <library key>.json (a sorted list), read by the feed, the TV and tools\\feed\\feed.py: one device can't wipe
what another sent. No file yet = nothing known.

POST /api/tv/known_lists (lists()) syncs the imported known words (Jiten) each device keeps on its own: the newest
import wins, in <library key>.lists.json. Marks sync through Anki (Progress).
"""
import json
import re
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


def _key(body: dict) -> str:
    person, lang = body.get("person") or OWNER, body.get("lang")
    key = PEOPLE.get(person, {}).get(lang) if isinstance(person, str) and isinstance(lang, str) else None
    if not key:
        raise ValueError("unknown person or language")
    return key


def _words(words) -> list[str]:
    if not isinstance(words, list) or len(words) > MAX_WORDS or not all(
            isinstance(w, str) and w.strip() and len(w) <= MAX_LEN and "\n" not in w for w in words):
        raise ValueError("words must be a list of short strings")
    return sorted({w.strip() for w in words})


def _write(f: Path, data) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)


def save(body: dict, folder: Path | None = None) -> dict:
    """Stores a device's list; the person's list becomes all their devices' together.
    ValueError when the person, language, device or words aren't right."""
    key, out, device = _key(body), _words(body.get("words")), body.get("device") or "unnamed"
    if not isinstance(device, str) or not re.fullmatch(r"[\w .-]{1,40}", device.strip()):
        raise ValueError("bad device")
    # ponytail: a device gone for good keeps its words in the union; delete its file in <key>.devices then.
    folder = folder or DIR
    devices = folder / f"{key}.devices"
    with _lock:
        _write(devices / f"{device.strip()}.json", out)
        union = sorted({w for f in devices.glob("*.json") for w in json.loads(f.read_text(encoding="utf-8"))})
        _write(folder / f"{key}.json", union)
    return {"ok": True, "key": key, "n": len(union)}


def lists(body: dict, folder: Path | None = None) -> dict:
    """Keeps the newest import of a person's devices. body: person, lang, imported {time (epoch ms), words (only when
    the PC may not have them)}. The answer's imported has words only when the PC's are newer than the device's.
    ValueError when something isn't right."""
    key, imp = _key(body), body.get("imported")
    if not isinstance(imp, dict) or not isinstance(imp.get("time"), int) or isinstance(imp["time"], bool):
        raise ValueError("imported needs a time")
    new = {"time": imp["time"], "words": _words(imp["words"])} if "words" in imp else None
    f = (folder or DIR) / f"{key}.lists.json"
    with _lock:
        old = json.loads(f.read_text(encoding="utf-8")).get("imported") if f.is_file() else None
        out = max((p for p in (old, new) if p), key=lambda p: p["time"], default=None)
        if out is not old:
            _write(f, {"imported": out})
    if out is None:
        return {}
    return {"imported": out if out["time"] > imp["time"] else {"time": out["time"]}}  # words only when newer


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
    assert save({"person": "pedro", "lang": "de", "device": "Boox", "words": ["Tag"]})["n"] == 3  # union, not a wipe
    assert save({"person": "pedro", "lang": "de", "device": "Boox", "words": []})["n"] == 2
    for bad in ({"person": "x", "lang": "de", "words": []}, {"person": "pedro", "lang": "ja", "words": []},
                {"person": "pedro", "lang": "de", "words": "haus"}, {"person": "pedro", "lang": "de", "words": ["a\nb"]},
                {"person": "pedro", "lang": "de", "words": [1]}, {"person": "pedro", "lang": "de", "words": ["x" * 65]}):
        try:
            save(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    print("ok")
