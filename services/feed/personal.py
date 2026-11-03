"""Each person's personal dictionary (kumapie_plan.md): words they added by selecting them in the player.

Every device keeps its own copy (it works offline) and sends all of it with POST /api/tv/personal
{"person": sync person, "entries": [...]}; the answer is the merged list. Per language and text the newest entry wins;
a deleted one stays with "deleted": true so it doesn't come back from a device that hasn't heard yet.
Kept in state/feed/personal/<person>.json.
"""
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # code/
from dojo import cfg  # noqa: E402

PEOPLE: dict[str, dict[str, str]] = cfg.raw.get("sync", {}).get("people", {})
# A device without an owner set is the feed person's (as the TV API treats it).
OWNER = next((p for p, langs in PEOPLE.items() if cfg.raw["feed"]["person"] in langs.values()), None)
DIR = cfg.state / "feed" / "personal"
MAX_ENTRIES = 20_000
MAX_LEN = {"lang": 8, "text": 64, "target": 64, "reading": 64, "meaning": 1000}

_lock = threading.Lock()


def clean(e) -> dict:
    """An entry as it is kept. ValueError when it isn't one."""
    if not isinstance(e, dict) or not isinstance(e.get("lang"), str) or not isinstance(e.get("text"), str) or not e["text"].strip():
        raise ValueError("an entry needs a language and a text")
    out = {k: e.get(k) or "" for k in MAX_LEN}
    if not all(isinstance(v, str) and len(v) <= MAX_LEN[k] for k, v in out.items()):
        raise ValueError("entry fields must be short strings")
    if not isinstance(e.get("time", 0), int) or isinstance(e.get("time"), bool):
        raise ValueError("an entry's time is a number")
    return out | {"time": e.get("time", 0), "deleted": e.get("deleted") is True}


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    """Both lists, the newest entry per language and text."""
    best: dict[tuple[str, str], dict] = {}
    for e in [*old, *new]:
        k = (e["lang"], e["text"])
        if k not in best or e["time"] >= best[k]["time"]:
            best[k] = e
    return sorted(best.values(), key=lambda e: (e["lang"], e["text"]))


def sync(body: dict, folder: Path = DIR, people=None) -> dict:
    """Merges a device's list into the person's; answers the result. ValueError when the person or entries aren't right."""
    person, entries = body.get("person") or OWNER, body.get("entries")
    if not isinstance(person, str) or person not in (PEOPLE if people is None else people):
        raise ValueError("unknown person")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ValueError("entries must be a list")
    entries = [clean(e) for e in entries]
    f = folder / f"{person}.json"
    with _lock:
        old = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
        out = merge(old, entries)
        if out != old:
            folder.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".part")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            tmp.replace(f)
    return {"entries": out}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        a = {"lang": "ja", "text": "可可阿", "target": "カカオ", "time": 1}
        go = lambda e: sync({"person": "p", "entries": e}, Path(d), {"p": {}})["entries"]
        assert [e["target"] for e in go([a])] == ["カカオ"]
        assert go([{**a, "target": "old", "time": 0}])[0]["target"] == "カカオ"  # an older copy loses
        assert go([{**a, "deleted": True, "time": 2}])[0]["deleted"] is True
        assert go([])[0]["deleted"] is True  # kept, for the devices that haven't heard
        for bad in ({"person": "x", "entries": []}, {"person": "p", "entries": [{"lang": "ja", "text": " "}]},
                    {"person": "p", "entries": [{"lang": "ja", "text": "a" * 65}]}):
            try:
                sync(bad, Path(d), {"p": {}})
            except ValueError:
                continue
            raise AssertionError(bad)
    print("ok")
