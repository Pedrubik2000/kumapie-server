"""Characters of the books and manga Pedro reads (Hoshi, chimahon), for the faces and names in their popups.

kumapie hears every work title those apps ask faces for (`face?name=…&work=…`) and posts them here; each new title is
looked up on AniList (a book or manga: type MANGA) and kept in state\\feed\\reading_works.json. The dictionary itself is
built by tools\\kumapie\\reading_characters.py (Bee's builder in WSL, so under Pedro's account, not this service's):
it writes state\\dicts\\yomitan-ja\\bee_reading_<stamp>.zip, which kumapie on the phones fetches and imports.

    POST /api/characters/works {"works": ["くま クマ 熊 ベアー17", "Okaeri Alice", ...]} -> {"added": n}
"""
import json
import re
import threading
import urllib.request
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dojo import cfg  # noqa: E402

STATE = cfg.state / "feed" / "reading_works.json"
_lock = threading.Lock()
SEARCH = "query($s:String){Page(perPage:5){media(search:$s,type:MANGA){id title{native romaji}}}}"


def load() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"works": {}, "built": []}


def _save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _norm(s: str) -> str:
    return re.sub(r"[\s　・〜~!！?？:：\-]", "", s or "").lower()


def _search(q: str) -> list:
    body = json.dumps({"query": SEARCH, "variables": {"s": q}}).encode()
    req = urllib.request.Request("https://graphql.anilist.co", body,
                                 {"Content-Type": "application/json", "User-Agent": "dojo-feed"})  # 403 without one
    return json.load(urllib.request.urlopen(req, timeout=20))["data"]["Page"]["media"]


def resolve(work: str) -> list[int]:
    """AniList ids for a work title: the best match, plus the other formats with the same title (novel + manga)."""
    base = re.sub(r"[\s　]*(第?\d+巻?|vol\.?\s*\d+|\(\d+\))\s*$", "", work.strip(), flags=re.I)  # "…ベアー17" -> "…ベアー"
    for q in dict.fromkeys([base, _norm(base), work]):
        if not q: continue
        found = _search(q)
        if found:
            top = _norm(found[0]["title"]["native"] or found[0]["title"]["romaji"])
            return [m["id"] for m in found if _norm(m["title"]["native"] or m["title"]["romaji"]) == top] or [found[0]["id"]]
    return []


def add(works: list[str]) -> dict:
    """New titles are looked up; one that AniList doesn't know is kept with no ids (not asked again)."""
    with _lock:
        d = load()
        new = [w for w in dict.fromkeys(w.strip() for w in works if isinstance(w, str)) if w and w not in d["works"]]
        for w in new[:20]:  # ponytail: 20 per call keeps AniList's 90/min limit; the rest come next time
            try:
                d["works"][w] = resolve(w)
            except OSError as e:
                print(f"characters: {w}: {e}", flush=True)
                break
        if new: _save(d)
        return {"added": len(new)}


def ids() -> list[int]:
    return sorted({i for v in load()["works"].values() for i in v})


if __name__ == "__main__":
    for w in sys.argv[1:]:
        print(w, "->", resolve(w))
