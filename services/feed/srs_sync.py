"""kumapie's own cards on the PC (kumapie_plan.md "kumapie's own Anki", step 4b): POST /api/tv/srs/sync.

state/feed/srs/<person>.sqlite has lang/Srs.kt's tables (made by C:\\dojo\\code\\tools\\srs\\import_kuma3.py; this
module never creates it). Devices keep one store per language and only append: revlog and exposure rows are kept by
id (a row sent twice is kept once), cards by id with the newest `modified` winning for their text. Every row the PC
takes gets a sequence number (table `sync`, PC only); a device asks for everything after the last one it saw.
The FSRS state columns on the PC's cards stay as the import left them: devices replay the revlog themselves (Srs.replayAll).

Request:  {"person", "lang", "since": seq (0 = everything), "cards": [...], "revlog": [...], "exposures": [...]}
Answer:   {"seq": newest seq, "meta": {key: value}, "cards": [...], "revlog": [...], "exposures": [...]}
          (the language's rows after `since`, without the ones this request sent).
"""
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root
from dojo import cfg  # noqa: E402
import sync  # noqa: E402

DIR = cfg.state / "feed" / "srs"
MAX_ROWS = 50_000
CARD = ["id", "note", "ord", "lang", "word", "sentence", "meaning", "fields", "tags", "show", "episode", "scene",
         "created", "modified"]
REVLOG = ["id", "card", "time", "rating", "answer_ms", "device", "s_before", "d_before", "s_after", "d_after", "r",
          "episode", "scene", "subs", "looked_up", "speed", "heard"]
EXPOSURE = ["id", "card", "time", "episode", "scene", "subs"]
TABLES = {"cards": CARD, "revlog": REVLOG, "exposures": EXPOSURE}
_lock = threading.Lock()


def _rows(body: dict, table: str) -> list[tuple]:
    rows = body.get(table) or []
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError(f"{table} must be a list")
    cols = TABLES[table]
    out = []
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("id"), int) or isinstance(r.get("id"), bool):
            raise ValueError(f"a {table} row needs an integer id")
        v = tuple(r.get(c) for c in cols)
        if not all(x is None or isinstance(x, (int, float, str)) for x in v):
            raise ValueError(f"{table} values are numbers or text")
        out.append(v)
    return out


def _open(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS sync (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, id INTEGER NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS sync_kind_id ON sync(kind, id)")
    if not db.execute("SELECT 1 FROM sync LIMIT 1").fetchone():  # first sync: the import's rows are seq 1..n
        for kind in TABLES:
            db.execute(f"INSERT INTO sync (kind, id) SELECT ?, id FROM {kind} ORDER BY id", (kind,))
    return db


def handle(body: dict, folder: Path = DIR, people=None) -> dict:
    """One device's sync. ValueError when the request isn't right or the person has no cards on the PC."""
    person, lang, since = body.get("person"), body.get("lang"), body.get("since", 0)
    if not isinstance(person, str) or person not in (sync.PEOPLE if people is None else people):
        raise ValueError("unknown person")
    if not isinstance(lang, str) or not isinstance(since, int) or isinstance(since, bool):
        raise ValueError("lang and since are needed")
    sent = {t: _rows(body, t) for t in TABLES}
    if any(r[3] != lang for r in sent["cards"]):
        raise ValueError("cards of another language")
    path = folder / f"{person}.sqlite"
    if not path.is_file():
        raise ValueError(f"no cards on the PC for {person} (tools\\srs\\import_kuma3.py)")
    with _lock:
        db = _open(path)
        try:
            with db:
                for t in ("revlog", "exposures"):
                    cols = TABLES[t]
                    for r in sent[t]:
                        if db.execute(f"INSERT OR IGNORE INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", r).rowcount:
                            db.execute("INSERT INTO sync (kind, id) VALUES (?, ?)", (t, r[0]))
                for r in sent["cards"]:
                    old = db.execute("SELECT modified FROM cards WHERE id = ?", (r[0],)).fetchone()
                    if old is None:
                        db.execute(f"INSERT INTO cards ({','.join(CARD)}) VALUES ({','.join('?' * len(CARD))})", r)
                    elif r[CARD.index("modified")] > old[0]:
                        db.execute(f"UPDATE cards SET {','.join(c + '=?' for c in CARD[1:])} WHERE id = ?", (*r[1:], r[0]))
                    else:
                        continue
                    db.execute("INSERT INTO sync (kind, id) VALUES (?, ?)", ("cards", r[0]))
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) FROM sync").fetchone()[0]
            mine = {t: {r[0] for r in rows} for t, rows in sent.items()}
            out = {"seq": seq, "meta": dict(db.execute("SELECT key, value FROM meta"))}
            for t, cols in TABLES.items():
                lang_of = "lang = ?" if t == "cards" else "card IN (SELECT id FROM cards WHERE lang = ?)"
                q = (f"SELECT {','.join(cols)} FROM {t} WHERE id IN (SELECT id FROM sync WHERE kind = ? AND seq > ?) "
                     f"AND {lang_of} ORDER BY id")
                out[t] = [dict(zip(cols, r)) for r in db.execute(q, (t, since, lang)) if r[0] not in mine[t]]
            return out
        except sqlite3.IntegrityError as e:  # a card without a column the table needs: 400, not 500
            raise ValueError(f"a row the PC's table refuses: {e}") from e
        finally:
            db.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        db = sqlite3.connect(Path(d) / "p.sqlite")  # the tables as Srs.kt makes them (only the columns used here)
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute(f"CREATE TABLE cards ({', '.join(c + (' NOT NULL' if c == 'word' else '') for c in CARD)}, stability, difficulty, last_review, due)")
        db.execute(f"CREATE TABLE revlog ({', '.join(REVLOG)})")
        db.execute(f"CREATE TABLE exposures ({', '.join(EXPOSURE)})")
        db.execute("INSERT INTO meta VALUES ('params', '[]')")
        card = lambda i, lang="de", mod=1, word="Haus": dict(zip(CARD, [i, i, 0, lang, word, "", "", "{}", "", None, None, None, 1, mod]))  # noqa: E731
        rev = lambda i, c: {"id": i, "card": c, "time": i, "rating": 3, "device": "x", "s_after": 1.0, "d_after": 5.0}  # noqa: E731
        db.execute(f"INSERT INTO cards ({','.join(CARD)}) VALUES ({','.join('?' * len(CARD))})", tuple(card(1).values()))
        db.execute(f"INSERT INTO cards ({','.join(CARD)}) VALUES ({','.join('?' * len(CARD))})", tuple(card(9, "ja").values()))
        db.commit()
        db.close()
        go = lambda **b: handle({"person": "p", "lang": "de", "since": 0} | b, Path(d), {"p": {}})  # noqa: E731
        first = go()
        assert [c["id"] for c in first["cards"]] == [1] and first["meta"] == {"params": "[]"}  # only German cards
        tablet = go(since=first["seq"], revlog=[rev(100, 1)], cards=[card(2, word="Baum")])
        assert tablet["revlog"] == [] and tablet["cards"] == []  # its own rows don't come back
        phone = go(since=first["seq"], revlog=[rev(50, 1)])  # an offline rating older than the tablet's
        assert [r["id"] for r in phone["revlog"]] == [100] and [c["id"] for c in phone["cards"]] == [2]
        again = go(since=phone["seq"])
        assert again["revlog"] == [] and again["cards"] == []
        assert [r["id"] for r in go(since=tablet["seq"])["revlog"]] == [50]  # the tablet hears of the phone's late row
        go(since=0, cards=[card(2, mod=0, word="old")])  # an older edit loses
        go(since=0, cards=[card(2, mod=5, word="Bäume")])  # a newer one wins
        assert next(c["word"] for c in go()["cards"] if c["id"] == 2) == "Bäume"
        all_ids = [r["id"] for r in go()["revlog"]]
        assert all_ids == [50, 100], all_ids  # each row once
        for bad in ({"person": "q"}, {"cards": [card(3, "ja")]}, {"revlog": [{"id": "1"}]}, {"since": True}, {"cards": [card(4, word=None)]}):
            try:
                go(**bad)
                raise AssertionError(f"accepted {bad}")
            except ValueError:
                pass
        try:
            handle({"person": "p", "lang": "de"}, Path(d) / "none", {"p": {}})
            raise AssertionError("no file accepted")
        except ValueError:
            pass
    print("srs_sync ok")
