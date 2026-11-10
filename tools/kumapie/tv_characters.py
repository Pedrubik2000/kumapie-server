"""Bee's Character Dictionary cut down to the shows in a person's Japanese library, for the TVs.

The whole dictionary (characterdictionary.tokyo, from the person's AniList list) is ~10 million entries, 22 GB of
JSON: the phones import it, a Chromecast (4 GB) can't. The TVs import what is in state\\dicts\\yomitan-ja, so this
writes a small zip there with only the characters of the library's shows; kumapie then shows 壬氏様 as one word.

  .venv\\Scripts\\python.exe tools\\kumapie\\tv_characters.py <bee_characters.zip> [--for pedro-ja]

Run it again after adding a Japanese show (the TVs fetch a zip by name once: the name carries the date and time).
"""
import argparse
import datetime
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dojo import cfg  # noqa: E402

OUT = cfg.state / "dicts" / "yomitan-ja"


def library_titles(person: str) -> dict[str, str]:
    """{Japanese title: show folder} of the person's anime, from the metadata the server keeps (AniList)."""
    titles = {}
    for show in sorted((cfg.library(person) / "anime").iterdir()):
        meta = cfg.state / "feed" / "meta" / f"{show.name}.json"
        ja = json.loads(meta.read_text(encoding="utf-8")).get("title", {}).get("ja") if meta.is_file() else None
        if ja:
            titles[ja] = show.name
    return titles


def source(glossary) -> str:
    """An entry's show: the text of its {"data": {"id": "source"}} node, without " From: "."""
    found = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if (n.get("data") or {}).get("id") == "source" and isinstance(n.get("content"), str):
                found.append(n["content"].replace("From:", "").strip())
            else:
                walk(n.get("content"))
    walk(glossary)
    return found[0] if found else ""


def images(glossary, out: set):
    if isinstance(glossary, list):
        for x in glossary:
            images(x, out)
    elif isinstance(glossary, dict):
        if glossary.get("tag") == "img" and glossary.get("path"):
            out.add(glossary["path"])
        images(glossary.get("content"), out)


def build(big: Path, person: str) -> Path:
    titles = library_titles(person)
    needles = [t.encode() for t in titles]  # as written in the bank: the zips keep UTF-8, not \u escapes
    kept, pics, shows = [], set(), {}
    with zipfile.ZipFile(big) as z:
        banks = [n for n in z.namelist() if n.startswith("term_bank_")]
        for i, name in enumerate(banks):
            raw = z.read(name)
            if not any(n in raw for n in needles) and not any(json.dumps(t)[1:-1].encode() in raw for t in titles):
                continue
            for term in json.loads(raw):
                show = source(term[5])
                if show in titles:  # the exact show: a sequel has its own title and its own entries
                    kept.append(term)
                    images(term[5], pics)
                    shows[show] = shows.get(show, 0) + 1
            if i % 500 == 0:
                print(f"{i}/{len(banks)} banks, {len(kept)} entries", flush=True)
        index = json.loads(z.read("index.json"))
        for k in ("downloadUrl", "indexUrl", "isUpdatable"):
            index.pop(k, None)  # a cut copy: not the one the site updates
        index["description"] = f"Character names, only the shows in the kumapie library ({len(shows)} titles)"
        OUT.mkdir(parents=True, exist_ok=True)
        for old in OUT.glob("bee_characters_library_*.zip"):
            old.unlink()
        out = OUT / f"bee_characters_library_{datetime.datetime.now():%Y%m%d_%H%M}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as o:
            o.writestr("index.json", json.dumps(index, ensure_ascii=False))
            for n in z.namelist():
                if n.startswith("tag_bank_"):
                    o.writestr(n, z.read(n))
            for b in range(0, len(kept), 2000):
                o.writestr(f"term_bank_{b // 2000 + 1}.json", json.dumps(kept[b:b + 2000], ensure_ascii=False))
            have = set(z.namelist())
            for p in sorted(pics & have):
                o.writestr(p, z.read(p))
    for t, folder in titles.items():
        print(f"{shows.get(t, 0):6d}  {t}  ({folder})")
    assert kept, "no entry matched: are the library's Japanese titles the dictionary's?"
    print(f"{out}: {len(kept)} entries, {len(pics)} pictures, {out.stat().st_size / 1e6:.1f} MB")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("zip", type=Path, help="the whole dictionary as downloaded from characterdictionary.tokyo")
    ap.add_argument("--for", dest="person", default="pedro-ja")
    a = ap.parse_args()
    build(a.zip, a.person)
