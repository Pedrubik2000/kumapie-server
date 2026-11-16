"""Builds the characters' dictionary of the books and manga in state\\feed\\reading_works.json (services\\feed\\characters.py).

Runs Bee's builder (our copy in WSL, ~/Japanese_Character_Name_Dictionary) for just those AniList ids, so it must
run under Pedro's account (dojo-feed's LocalSystem has no WSL). Does nothing when the ids haven't changed. Writes
state\\dicts\\yomitan-ja\\bee_reading_<stamp>.zip titled "Bee's Characters (books & manga)" (not Bee's own title: the
phones' whole Bee dictionary would be replaced) and deletes the older one; kumapie on the phones fetches it.

    .venv\\Scripts\\python.exe tools\\kumapie\\reading_characters.py [--force]
"""
import datetime
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "feed"))
from dojo import cfg  # noqa: E402
import characters  # noqa: E402

OUT = cfg.state / "dicts" / "yomitan-ja"
PORT = 3056
TITLE = "Bee's Characters (books & manga)"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # the task runs it with pythonw: no console flashes on the PC


def _aliases(works: dict) -> dict[str, list[str]]:
    """{AniList native title: the app titles that resolved to it}, for titles the apps send differently."""
    media = {}
    for work, ms in works.items():
        for m in ms:
            media.setdefault(int(str(m).split(":")[-1]), []).append(work)
    if not media:
        return {}
    q = "query($ids:[Int]){Page(perPage:50){media(id_in:$ids){id title{native}}}}"
    req = urllib.request.Request("https://graphql.anilist.co", json.dumps({"query": q, "variables": {"ids": list(media)}}).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "dojo-feed"})
    out = {}
    for m in json.load(urllib.request.urlopen(req, timeout=30))["data"]["Page"]["media"]:
        native = m["title"]["native"]
        if native:
            out.setdefault(native, [])
            out[native] += [w for w in media[m["id"]] if w not in out[native] and w != native]
    return {k: v for k, v in out.items() if v}


HONORIFICS = {"さん": ("さん", "Generic polite suffix (Mr./Ms./Mrs.)"), "様": ("さま", "Very formal/respectful"),
              "殿": ("どの", "Formal (lord)"), "ちゃん": ("ちゃん", "Endearing, for close friends or children")}


def _bare(bank: list, consorts: set, terms: set) -> list:
    """AniList names Kusuriya's consorts with their title (玉葉妃); the lines also say just 玉葉 (user, 2026-11-16):
    every entry of such a name again without 妃 (玉葉, 玉葉さん, ...), where the dictionary has none."""
    out = []
    for t in bank:
        i = t[0].find("妃")
        w = t[0][:i] + t[0][i + 1:] if i > 0 and t[0][:i] in consorts else None
        if w and w not in terms:
            terms.add(w)
            out.append([w, *t[1:]])
    return out


def _extra(terms: set) -> list:
    """Characters AniList doesn't have (extra_characters.json), shaped like Bee's entries: kumapie reads the name, its
    "name-romaji" line, an honorific's own first line and "From: <title>" (that show's faces and names)."""
    src = json.loads(Path(__file__).with_name("extra_characters.json").read_text(encoding="utf-8"))
    out = []
    for title, people in src.items():
        if title == "_":
            continue
        for name, reading, romaji, role in people:
            body = [{"tag": "div", "content": name, "data": {"id": "name"}, "style": {"fontSize": "1.2em", "fontWeight": "bold"}},
                    {"tag": "div", "content": romaji, "data": {"id": "name-romaji"}, "style": {"color": "#666", "fontStyle": "italic"}},
                    {"tag": "div", "data": {"id": "role-container"}, "content": [
                        {"tag": "span", "content": role, "data": {"id": "role"}},
                        {"tag": "span", "content": f" From: {title}", "data": {"id": "source"}}]}]
            forms = [(name, reading, [])] + [
                (name + h, reading + kana, [{"tag": "div", "content": [{"tag": "span", "content": h}, {"tag": "span", "content": f" — {why}"}]}])
                for h, (kana, why) in HONORIFICS.items()]
            for w, r, head in forms:
                if w not in terms:
                    terms.add(w)
                    out.append([w, r, "name side", "", 50, [{"type": "structured-content", "content": head + body}], 0, ""])
    return out


def main(force: bool) -> None:
    ids = characters.ids()
    d = characters.load()
    have = sorted(OUT.glob("bee_reading_*.zip"))
    if not ids or (ids == d.get("built") and have and not force):
        print("nothing new")
        return
    server = subprocess.Popen(["wsl", "-e", "bash", "-lc",
                               f"cd ~/Japanese_Character_Name_Dictionary/yomitan-dict-builder && PORT={PORT} "
                               "CACHE_DIR=$HOME/.cache/bee-dict exec ./target/release/yomitan-dict-builder"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2)
                break
            except OSError:
                time.sleep(1)
        entries = json.dumps([{"source": "anilist", "id": i.split(":")[1], "media_type": i.split(":")[0]} for i in ids])
        tmp = OUT / "bee_reading.part"
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/yomitan-dict?entries={urllib.parse.quote(entries)}",
                                    timeout=3600) as r, open(tmp, "wb") as f:
            f.write(r.read())
    finally:
        subprocess.run(["wsl", "-e", "pkill", "-f", "target/release/yomitan-dict-builder"], capture_output=True, creationflags=NO_WINDOW)
        server.wait(timeout=30)
    aliases = _aliases(d["works"])
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    out = OUT / f"bee_reading_{stamp}.zip"
    part = OUT / f"bee_reading_{stamp}.zip.tmp"  # the devices list *.zip: never a half-written one
    with zipfile.ZipFile(tmp) as src, zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as dst:
        banks = [i.filename for i in src.infolist() if i.filename.startswith("term_bank_")]
        terms = {t[0] for b in banks for t in json.loads(src.read(b))}
        consorts = {w[:-1] for w in terms if w.endswith("妃") and len(w) > 2 and w[:-1] not in terms}  # 玉葉妃 -> 玉葉
        extra = [t for b in banks for t in _bare(json.loads(src.read(b)), consorts, terms)] + _extra(terms)
        items = src.infolist() + ([zipfile.ZipInfo("term_bank_9999.json")] if extra else [])
        for item in items:
            data = src.read(item.filename) if item.filename != "term_bank_9999.json" else \
                json.dumps(extra, ensure_ascii=False).encode()
            if item.filename == "index.json":
                index = json.loads(data)
                index["title"] = TITLE
                index["revision"] = stamp
                for k in ("downloadUrl", "indexUrl", "isUpdatable"):  # 127.0.0.1 links mean nothing on a phone
                    index.pop(k, None)
                data = json.dumps(index, ensure_ascii=False, indent=2).encode()
            elif item.filename.startswith("term_bank_") and aliases:
                # kumapie shows a face only when the app's title matches "From: <title>"; chimahon sends romaji
                # folder names (Chainsaw Man), Bee writes the Japanese title (チェンソーマン): add the app's titles.
                text = data.decode("utf-8")
                for native, works in aliases.items():
                    text = text.replace(f"From: {native}\"", f"From: {native} ({' / '.join(works)})\"")
                data = text.encode("utf-8")
            dst.writestr(item, data, zipfile.ZIP_DEFLATED)
    tmp.unlink()
    part.replace(out)
    for old in have:
        try:
            old.unlink()
        except PermissionError:  # an app still reading it (2026-11-16): next build removes it
            pass
    d = characters.load()  # the service may have added works meanwhile: keep them
    d["built"] = ids
    characters._save(d)
    print(f"{out.name}: {len(ids)} AniList titles, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    if sys.stdout is None:  # pythonw (the dojo-reading-characters task): to work\logs instead
        sys.stdout = sys.stderr = open(cfg.root / "work" / "logs" / "reading-characters.log", "a", encoding="utf-8", buffering=1)
        print(datetime.datetime.now().strftime("%F %T"), end=" ")
    main("--force" in sys.argv)
