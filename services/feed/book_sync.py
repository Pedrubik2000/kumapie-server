"""Books and manga added on a device, kept on the PC and offered to the person's other devices (as sync.py does episodes).

A book is the app's own folder (kumapie-tv mobile/read/Books.kt): book.json ({title, lang, paragraphs, pages}) and, for
manga, pages/NNNN.<ext>. On the PC it lives in library\\<key>\\manga\\<series>\\<volume>\\ (manga) or
library\\<key>\\light_novels\\<title>\\ (text, EPUB), book.json carrying its "id". The library is the index.

  GET  /api/sync/<person>/books                    [{id, title, series, lang, kind, pages, size, uploaded}], newest first
  GET  /api/sync/<person>/books/<id>/<file>         book.json | pages/NNNN.jpg (a device reading without its own copy)
  GET  /api/sync/<person>/books/<id>/status         {file: bytes received so far}
  PUT  /api/sync/<person>/books/<id>/pages/<f>?offset=N  the next bytes of a page (409 + {"size": n} when N is wrong)
  POST /api/sync/<person>/books/<id>/done           {title, series, lang, book: book.json}: into the library, listed
                                                    (book.json comes last and whole: the device may change it meanwhile)
  POST /api/sync/<person>/books/<id>/delete         "Delete everywhere": to archive/removed/<date>/, its id in
                                                    /api/sync/<person>/deleted (sync.py) so the other devices drop it
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from dojo import cfg
from dojo.config import safe_name

import sync

ID = re.compile(r"^book-[0-9a-f]{12}$")
FILE = re.compile(r"^(book\.json|pages/\d{4}\.(jpe?g|png|webp|avif|gif))$")
KINDS = ("manga", "light_novels")
UPLOADS = sync.STATE / "sync-upload-books"


def valid(person: str, bid: str, name: str | None = None) -> bool:
    return person in sync.PEOPLE and bool(ID.match(bid)) and (name is None or bool(FILE.match(name)))


def _all(person: str) -> dict[str, tuple[Path, dict]]:
    """{id: (folder, book.json)} for the person's libraries. ponytail: reads every book.json per call; index it if slow."""
    out = {}
    for key in sync.PEOPLE.get(person, {}).values():
        for kind in KINDS:
            for f in (cfg.library(key) / kind).rglob("book.json"):
                try:
                    b = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if ID.match(str(b.get("id", ""))):
                    out[b["id"]] = (f.parent, b | {"kind": kind})
    return out


def listing(person: str) -> list[dict]:
    rows = [{"id": bid, "title": b.get("title", ""), "series": b.get("series", ""), "lang": b.get("lang", ""),
             "kind": b["kind"], "pages": len(b.get("pages") or []), "uploaded": b.get("uploaded", ""),
             "size": sum(f.stat().st_size for f in d.rglob("*") if f.is_file())}
            for bid, (d, b) in _all(person).items()]
    return sorted(rows, key=lambda r: r["uploaded"], reverse=True)


def stored(person: str, bid: str, name: str) -> Path | None:
    d = _all(person).get(bid, (None,))[0]
    f = d / name if d else None
    return f if f and f.is_file() else None


def status(person: str, bid: str) -> dict:
    d = UPLOADS / person / bid
    return {f.relative_to(d).as_posix(): f.stat().st_size for f in d.rglob("*") if f.is_file()} if d.is_dir() else {}


def receive(person: str, bid: str, name: str, offset: int, read, length: int) -> int:
    f = UPLOADS / person / bid / name
    f.parent.mkdir(parents=True, exist_ok=True)
    have = f.stat().st_size if f.exists() else 0
    if offset != have:
        raise ValueError(have)
    with f.open("ab") as out:
        left = length
        while left > 0:
            chunk = read(min(left, 1 << 20))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)
    return f.stat().st_size


def finish(person: str, bid: str, meta: dict) -> dict:
    """Moves a complete upload into the library. ValueError when it can't (missing files, no library for the language)."""
    d = UPLOADS / person / bid
    if isinstance(meta.get("book"), dict):
        d.mkdir(parents=True, exist_ok=True)
        (d / "book.json").write_text(json.dumps(meta["book"], ensure_ascii=False), encoding="utf-8")
    if not (d / "book.json").is_file():
        raise ValueError("book.json is needed")
    book = json.loads((d / "book.json").read_text(encoding="utf-8"))
    missing = [p["image"] for p in book.get("pages") or [] if not (d / p["image"]).is_file()]
    if missing:
        raise ValueError(f"{len(missing)} pages missing, first {missing[0]}")
    lang = str(meta.get("lang") or book.get("lang") or "ja")
    key = sync.PEOPLE[person].get(lang)
    if not key:
        raise ValueError(f"no library for {person} in {lang}")
    title = str(meta.get("title") or book.get("title") or bid)
    series = str(meta.get("series") or title)
    with sync._lock:
        old = _all(person).get(bid)
        if old:  # uploaded before (another device has the same volume): this copy is already there
            shutil.rmtree(d, ignore_errors=True)
            return {"id": bid, "title": title, "existing": True}
        kind = "manga" if book.get("pages") else "light_novels"
        out = cfg.library(key) / kind / safe_name(sync._slug(series))
        if kind == "manga":
            out /= safe_name(sync._slug(title))
        if out.exists():
            out = out.with_name(f"{out.name}_{bid[5:]}")
        book.update(id=bid, series=series, uploaded=time.strftime("%Y-%m-%dT%H:%M:%S"))
        (d / "book.json").write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), out)
    return {"id": bid, "title": title, "kind": kind}


def delete(person: str, bid: str) -> dict:
    """Off the PC (to archive/removed/<date>/) and remembered, so the other devices drop it too."""
    with sync._lock:
        d, b = _all(person).get(bid, (None, {}))
        if d:
            out = cfg.archive / "removed" / time.strftime("%Y-%m-%d") / d.relative_to(cfg.library_root)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), out)
            if d.parent.name not in KINDS and not any(d.parent.iterdir()):  # the series' last volume
                d.parent.rmdir()
        sync.remember_deleted(person, bid)
    return {"id": bid, "deleted": bool(d), "title": b.get("title", "")}


if __name__ == "__main__":  # self-check: upload -> library -> listing -> delete
    import tempfile, types
    assert valid("pedro", "book-0123456789ab", "pages/0007.jpg") and not valid("pedro", "book-0123456789ab", "../x")
    assert not valid("pedro", "book-0123456789ab", "pages/../../x.jpg")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sync.PEOPLE = {"pedro": {"ja": "pedro-ja"}}
        cfg = types.SimpleNamespace(library=lambda k: root / k, library_root=root, archive=root / "archive")
        UPLOADS, sync.DELETED = root / "up", root / "deleted.json"
        bid = "book-0123456789ab"
        page = b"\xff\xd8jpeg"
        body = json.dumps({"title": "Yotsuba Volume 01", "lang": "ja", "paragraphs": [],
                           "pages": [{"image": "pages/0000.jpg", "w": 1, "h": 1, "blocks": None}]}).encode()
        for name, data in (("book.json", body), ("pages/0000.jpg", page)):
            assert receive("pedro", bid, name, 0, __import__("io").BytesIO(data).read, len(data)) == len(data)
        assert status("pedro", bid) == {"book.json": len(body), "pages/0000.jpg": len(page)}
        try:
            receive("pedro", bid, "book.json", 0, None, 0); raise AssertionError("wrong offset accepted")
        except ValueError as e:
            assert e.args[0] == len(body)
        assert finish("pedro", bid, {"series": "Yotsuba"})["kind"] == "manga"
        assert (root / "pedro-ja/manga/yotsuba/yotsuba_volume_01/pages/0000.jpg").read_bytes() == page
        [row] = listing("pedro")
        assert row["id"] == bid and row["pages"] == 1 and row["series"] == "Yotsuba"
        assert stored("pedro", bid, "pages/0000.jpg").read_bytes() == page
        assert delete("pedro", bid)["deleted"] and listing("pedro") == [] and sync.deleted("pedro") == [bid]
        assert not (root / "pedro-ja/manga/yotsuba").exists()
    print("ok")
