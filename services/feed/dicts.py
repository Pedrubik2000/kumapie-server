"""
Short word meanings for the TV from the same Yomitan dictionaries the phones use (kumapie_plan.md item 16).

The phones look words up live and never save the meanings, so episodes made on a phone (or by pc_episodes.py) reach
the PC with none. The PC fills them from these dictionaries: config.toml [kumapie.dicts] maps "<spoken>-<meanings>"
(de-en, en-es, ...) to a Yomitan zip, and `build` turns each into rows of state\\feed\\dicts.db:

    .venv\\Scripts\\python.exe services\\feed\\dicts.py build        (a few minutes; again after changing a zip)
    .venv\\Scripts\\python.exe services\\feed\\dicts.py look ja-en 食べる

A meaning is the dictionary's first senses joined, short (the TV card's "word = gloss" style): the general meaning,
not the sense in this line (the phone's dictionary panel picks that).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dojo import cfg  # noqa: E402

DB = cfg.state / "feed" / "dicts.db"
SENSES = 3        # senses joined into one meaning
MAX_LEN = 90      # characters, cut at a sense boundary when possible
SKIP = {"preamble", "tags", "tag", "backlink", "attribution", "part-of-speech-info", "example-sentence", "extra-info",
        "related-words", "info-gloss", "forms", "xref", "antonym", "references", "notes"}


def _text(node) -> str:
    """The visible text of a structured-content node, without examples, notes, links or tags."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_text(n) for n in node)
    if not isinstance(node, dict) or node.get("tag") in ("summary", "details", "a", "rt", "img"):
        return ""
    if (node.get("data") or {}).get("content") in SKIP:
        return ""
    return _text(node.get("content", ""))


def _senses(node, out: list[str]) -> None:
    """wty: each <li> of an ol "glosses" is a sense (its first part; sub-senses and examples follow it).
    Jitendex: each ul "glossary" is a sense, its <li>s are synonyms."""
    if isinstance(node, list):
        for n in node:
            _senses(n, out)
        return
    if not isinstance(node, dict):
        return
    kind = (node.get("data") or {}).get("content")
    items = node.get("content")
    items = items if isinstance(items, list) else [items]
    if kind == "glosses":
        for li in items:
            first = li.get("content") if isinstance(li, dict) else li
            first = first[0] if isinstance(first, list) and first else first
            out.append(_text(first).strip().split("\n")[0])
    elif kind == "glossary":
        out.append(", ".join(t for t in (_text(li).strip() for li in items) if t))
    elif kind not in SKIP:
        _senses(node.get("content"), out)


def meaning(glossary: list) -> list[str]:
    out: list[str] = []
    for g in glossary:
        if isinstance(g, str):
            out.append(g)
        elif isinstance(g, dict) and g.get("type") == "structured-content":
            _senses(g.get("content"), out)
        elif isinstance(g, dict) and g.get("type") == "text":
            out.append(g.get("text", ""))
    return [s for s in out if s]


def join(senses: list[str]) -> str:
    text = ""
    for s in dict.fromkeys(senses):  # in order, no repeats
        nxt = f"{text}; {s}" if text else s
        if text and len(nxt) > MAX_LEN:
            break
        text = nxt
    return text if len(text) <= MAX_LEN + 30 else text[:MAX_LEN].rstrip(" ,;") + "…"


def build() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.execute("create table m (pair text, key text, reading integer, score integer, gloss text)")
    for pair, rel in cfg.raw["kumapie"]["dicts"].items():
        zf = zipfile.ZipFile(cfg.root / rel)
        terms: dict[tuple[str, int], list] = {}  # (key, 1 if it's a reading) -> [score, senses]
        for name in sorted(n for n in zf.namelist() if n.startswith("term_bank")):
            for term, reading, _tags, _rules, score, glossary, *_ in json.loads(zf.read(name)):
                senses = meaning(glossary)
                if not senses:
                    continue
                for key, is_reading in ((term, 0), (reading, 1)) if reading and reading != term else ((term, 0),):
                    have = terms.setdefault((key, is_reading), [score, []])
                    if score > have[0]:  # the most common entry first (Jitendex scores; wty has none)
                        have[:] = [score, []]
                    if len(have[1]) < SENSES:
                        have[1] += senses[:SENSES - len(have[1])]
        db.executemany("insert into m values (?,?,?,?,?)",
                       ((pair, k, r, sc, join(ss)) for (k, r), (sc, ss) in terms.items()))
        print(f"{pair}: {len(terms)} keys from {rel}", flush=True)
    db.execute("create index m_key on m (pair, key)")
    db.commit()
    db.close()
    tmp.replace(DB)


_cache: dict[tuple[str, str], str] = {}
_lock = threading.Lock()
_db: sqlite3.Connection | None = None


def look(pair: str, *keys: str) -> str:
    """The meaning of the first key found (a word's dictionary form, then the form in the line); "" when none."""
    global _db
    with _lock:  # one read-only connection shared by the server's threads
        if _db is None:
            if not DB.is_file():
                return ""
            _db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
        for key in dict.fromkeys(k for k in keys if k):
            for k in dict.fromkeys((key, key.lower(), key[:1].upper() + key[1:])):  # German nouns, sentence starts
                if (pair, k) not in _cache:
                    row = _db.execute("select gloss from m where pair = ? and key = ? order by reading, score desc limit 1",
                                      (pair, k)).fetchone()
                    _cache[(pair, k)] = row[0] if row else ""
                if _cache[(pair, k)]:
                    return _cache[(pair, k)]
    return ""


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build()
    elif sys.argv[1:2] == ["look"]:
        print(look(sys.argv[2], *sys.argv[3:]) or "(none)")
    else:
        print(__doc__)
