"""
Short word meanings for the TV from the same Yomitan dictionaries the phones use (kumapie_plan.md item 16).

The phones look words up live and never save the meanings, so episodes made on a phone (or by pc_episodes.py) reach
the PC with none. The PC fills them from these dictionaries: config.toml [kumapie.dicts] maps "<spoken>-<meanings>"
(de-en, en-es, ...) to a Yomitan zip, and `build` turns each into rows of state\\feed\\dicts.db:

    .venv\\Scripts\\python.exe services\\feed\\dicts.py build        (a few minutes; again after changing a zip)
    .venv\\Scripts\\python.exe services\\feed\\dicts.py look ja-en 食べる

A meaning is the dictionary's first senses joined, short (the TV card's "word = gloss" style): the general meaning.
Each row also keeps up to KEEP senses and the kana reading, so an episode can give each scene the sense that fits
its English (`senses` + `best`, like the phone's SensePick) and the TV can show the reading (user, 2026-10-14).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dojo import cfg  # noqa: E402

DB = cfg.state / "feed" / "dicts.db"
SENSES = 3        # senses joined into one meaning
KEEP = 12         # senses kept for picking the one that fits a scene
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
    db.execute("create table m (pair text, key text, reading integer, score integer, gloss text, senses text, kana text)")
    for pair, rel in cfg.raw["kumapie"]["dicts"].items():
        zf = zipfile.ZipFile(cfg.root / rel)
        terms: dict[tuple[str, int], list] = {}  # (key, 1 if it's a reading) -> [score, senses, kana]
        for name in sorted(n for n in zf.namelist() if n.startswith("term_bank")):
            for term, reading, _tags, _rules, score, glossary, *_ in json.loads(zf.read(name)):
                senses = meaning(glossary)
                if not senses:
                    continue
                for key, is_reading in ((term, 0), (reading, 1)) if reading and reading != term else ((term, 0),):
                    have = terms.setdefault((key, is_reading), [score, [], reading or term])
                    if score > have[0]:  # the most common entry first (Jitendex scores; wty has none)
                        have[:] = [score, [], reading or term]
                    if len(have[1]) < KEEP:
                        have[1] += senses[:KEEP - len(have[1])]
        db.executemany("insert into m values (?,?,?,?,?,?,?)",
                       ((pair, k, r, sc, join(ss[:SENSES]), json.dumps(list(dict.fromkeys(ss)), ensure_ascii=False), kana)
                        for (k, r), (sc, ss, kana) in terms.items()))
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


_scache: dict[tuple[str, str], tuple[str, list[str]]] = {}


def senses(pair: str, *keys: str) -> tuple[str, list[str]]:
    """The kana reading and the senses of the first key found; ("", []) when none (or a dicts.db built before them)."""
    global _db
    with _lock:
        if _db is None:
            if not DB.is_file():
                return "", []
            _db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
        for key in dict.fromkeys(k for k in keys if k):
            if (pair, key) not in _scache:
                try:
                    row = _db.execute("select kana, senses from m where pair = ? and key = ? order by reading, score desc limit 1",
                                      (pair, key)).fetchone()
                except sqlite3.OperationalError:  # no senses column yet: `build` again
                    row = None
                _scache[(pair, key)] = (row[0] or "", json.loads(row[1] or "[]")) if row else ("", [])
            if _scache[(pair, key)][1]:
                return _scache[(pair, key)]
    return "", []


_STOP = set(("to a an the of or and be is in on for with by as e g etc someone something one's it that this at from up out "
             "not used i you he she we they me my your his her its our their do does have has").split())
# Light verbs: in subtitles mostly idioms ("Give me a break!" isn't 取る "to give"), so they make no match.
_STOP |= {"give", "get", "take", "make", "go", "come", "put", "let", "keep", "break", "way"}


def _words(text: str) -> set[str]:
    def stem(w: str) -> str:
        for suffix in ("ing", "ed", "es", "s", "ly"):
            if len(w) > 4 and w.endswith(suffix):
                return w[:-len(suffix)]
        return w
    return {stem(w) for w in re.findall(r"[a-z']+", text.lower()) if w not in _STOP}


def best(sense_list: list[str], english: str) -> str | None:
    """The sense sharing the most words with [english] (words before a "(" count double), or None: the phone's
    SensePick.bestMeaning. English meanings only (ponytail: the parents' Spanish ones would need SensePick's es rules)."""
    en = _words(english)
    top, pick = 0, None
    for s in sense_list:
        score = 2 * len(_words(s.split("(")[0]) & en) + len(_words(s) & en)
        if score > top:
            top, pick = score, s
    return pick


if __name__ == "__main__":
    assert best(["to take, to pick up", "to pass, to hand, to give", "to pick, to gather, to harvest"],
                "Give me a break! We were out gathering materials.") == "to pick, to gather, to harvest"
    assert best(["to take", "to pass, to give"], "Give me a break!") is None
    if sys.argv[1:2] == ["build"]:
        build()
    elif sys.argv[1:2] == ["look"]:
        print(look(sys.argv[2], *sys.argv[3:]) or "(none)")
    else:
        print(__doc__)
