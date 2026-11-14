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
SEARCH = "query($s:String,$t:MediaType){Page(perPage:10){media(search:$s,type:$t){id type title{native romaji}}}}"


def load() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"works": {}, "built": []}


def _save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _norm(s: str) -> str:
    return re.sub(r"[\s　・〜~!！?？:：\-]", "", s or "").lower()


def _search(q: str, kind: str) -> list:
    body = json.dumps({"query": SEARCH, "variables": {"s": q, "t": kind}}).encode()
    req = urllib.request.Request("https://graphql.anilist.co", body,
                                 {"Content-Type": "application/json", "User-Agent": "dojo-feed"})  # 403 without one
    return json.load(urllib.request.urlopen(req, timeout=20))["data"]["Page"]["media"]


def _title(m) -> str:
    return _norm(m["title"]["native"] or m["title"]["romaji"])


def resolve(work: str) -> list[str]:
    """AniList media for a work title as "MANGA:97929" / "ANIME:127550": the best book/manga match and every book,
    manga or anime whose title starts with its title (novel + manga + spin-offs + the anime: AniList puts some
    characters only on the anime, like くまクマ熊ベアー's セニア)."""
    base = re.sub(r"[\s　]*(第?\d+巻?|vol\.?\s*\d+|\(\d+\))\s*$", "", work.strip(), flags=re.I)  # "…ベアー17" -> "…ベアー"
    for q in dict.fromkeys([base, _norm(base), work]):
        if not q: continue
        found = _search(q, "MANGA")
        if found:
            top = _title(found[0])
            same = [m for m in found + _search(q, "ANIME") if _title(m).startswith(top)]
            return [f"{m['type']}:{m['id']}" for m in same] or [f"MANGA:{found[0]['id']}"]
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
        if any(d["works"].get(w) for w in new):
            build()
        return {"added": len(new)}


def build() -> None:
    """Starts the dojo-reading-characters task (reading_characters.py under Pedro's account: WSL isn't this service's).
    kumapie picks the new dictionary up on its next start."""
    import subprocess
    r = subprocess.run(["schtasks", "/run", "/tn", "dojo-reading-characters"], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"characters: build task {'started' if r.returncode == 0 else 'failed: ' + r.stderr.strip()}", flush=True)


def ids() -> list[str]:
    """Every work's media; an old plain id (before anime were added) counts as MANGA."""
    return sorted({i if isinstance(i, str) else f"MANGA:{i}" for v in load()["works"].values() for i in v})


def refresh() -> None:
    """Looks every known title up again (after the lookup changed)."""
    with _lock:
        d = load()
        for w in d["works"]:
            d["works"][w] = resolve(w)
        _save(d)


def alias(work: str, search: str) -> list[str]:
    """An app title AniList can't find (chimahon's folder name "Okaeriarisu") looked up as another ("Okaeri Alice")."""
    with _lock:
        d = load()
        d["works"][work] = resolve(search)
        _save(d)
        return d["works"][work]


if __name__ == "__main__":
    # characters.py <title>...          : what each title resolves to
    # characters.py as <app title> <search title> : look the app's title up as another
    if sys.argv[1:2] == ["as"]:
        print(sys.argv[2], "->", alias(sys.argv[2], sys.argv[3]))
    else:
        for w in sys.argv[1:]:
            print(w, "->", resolve(w))
