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
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        subprocess.run(["wsl", "-e", "pkill", "-f", "target/release/yomitan-dict-builder"], capture_output=True)
        server.wait(timeout=30)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    out = OUT / f"bee_reading_{stamp}.zip"
    with zipfile.ZipFile(tmp) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "index.json":
                index = json.loads(data)
                index["title"] = TITLE
                index["revision"] = stamp
                for k in ("downloadUrl", "indexUrl", "isUpdatable"):  # 127.0.0.1 links mean nothing on a phone
                    index.pop(k, None)
                data = json.dumps(index, ensure_ascii=False, indent=2).encode()
            dst.writestr(item, data)
    tmp.unlink()
    for old in have:
        old.unlink()
    d = characters.load()  # the service may have added works meanwhile: keep them
    d["built"] = ids
    characters._save(d)
    print(f"{out.name}: {len(ids)} AniList titles, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main("--force" in sys.argv)
