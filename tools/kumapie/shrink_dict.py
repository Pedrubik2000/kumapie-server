"""A Yomitan picture dictionary with its pictures shrunk to popup size (素材辞典: 1 GB -> a tenth), for the devices.

  C:\\dojo\\code\\.venv\\Scripts\\python.exe tools\\kumapie\\shrink_dict.py <dictionary.zip>... [--side 400] [--out <folder>]
  (that venv has Pillow; this repo's doesn't)

Every jpg/png/webp becomes a WebP no larger than --side pixels, named <old name>.webp, and the term banks point to
the new names. Other files (gif, svg, avif, css) are copied as they are. Writes "[Pictures] <title>.zip": kumapie
takes the popup's chip from the brackets.

--pictured: for a whole dictionary that also has pictures (新世紀ビジュアル大辞典: 148,045 entries, 5,822 with one):
only the entries with a picture, and only the files they use.
"""
import argparse
import io
import json
import re
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dojo import cfg  # noqa: E402
from dojo.config import safe_name  # noqa: E402

OUT = cfg.state / "dicts" / "pictures"
RASTER = (".jpg", ".jpeg", ".png", ".webp")


def shrink(data: bytes, side: int) -> bytes | None:
    """The picture as WebP within side x side, or None when it can't be read (then the original is kept)."""
    try:
        im = Image.open(io.BytesIO(data))
        if getattr(im, "n_frames", 1) > 1:
            return None  # animated: keep it moving
        im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
        im.thumbnail((side, side), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "WEBP", quality=75, method=4)
        return out.getvalue()
    except Exception:
        return None


MARK = re.compile(r"gaiji|font|logo|icon|hitsujun", re.I)  # letters and marks drawn as images, not pictures


def paths(node, out: set):
    if isinstance(node, list):
        for x in node:
            paths(x, out)
    elif isinstance(node, dict):
        if isinstance(node.get("path"), str):
            out.add(node["path"])
        paths(node.get("content"), out)


def pictured(term) -> bool:
    used = set()
    paths(term[5], used)
    return any(p.lower().endswith(RASTER) and not MARK.search(p) for p in used)


def repoint(node, renamed: dict):
    """Paths follow their new names; letters drawn as images (EPWING gaiji, no size given) get one em, in the text
    colour, or chimahon draws them 100em wide and Hoshi not at all. Monochrome paints through the background layer,
    so background stays on (off, chimahon shows nothing)."""
    if isinstance(node, list):
        for x in node:
            repoint(x, renamed)
    elif isinstance(node, dict):
        if node.get("path") in renamed:
            node["path"] = renamed[node["path"]]
        if node.get("tag") == "img" and MARK.search(node.get("path", "")) and "width" not in node and "height" not in node:
            node.update(width=1, height=1, sizeUnits="em", appearance="monochrome", background=True, verticalAlign="text-bottom")
        repoint(node.get("content"), renamed)


def build(src: Path, side: int, out_dir: Path, only_pictured: bool = False) -> Path:
    with zipfile.ZipFile(src) as z:
        title = json.loads(z.read("index.json"))["title"]
        out = out_dir / f"[Pictures] {safe_name(title)}.zip"
        names = [n for n in z.namelist() if not n.endswith("/")]
        banks = {n: json.loads(z.read(n)) for n in names if n.endswith(".json") and "_bank_" in n}
        if only_pictured:
            used = set()
            for n in [n for n in banks if "term_bank" in n]:
                banks[n] = [t for t in banks[n] if pictured(t)]
                paths(banks[n], used)
            names = [n for n in names if n in used or n in banks and banks[n] or not n.lower().endswith(RASTER + (".json",))
                     or n == "index.json"]
        pics = [n for n in names if n.lower().endswith(RASTER)]
        with ProcessPoolExecutor() as pool:
            small = dict(zip(pics, pool.map(shrink, (z.read(n) for n in pics), [side] * len(pics), chunksize=32)))
        renamed = {n: n + ".webp" for n, b in small.items() if b is not None}
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as o:
            for n in names:
                if n in renamed:
                    o.writestr(zipfile.ZipInfo(renamed[n]), small[n])  # stored: WebP doesn't deflate
                elif n in banks:
                    repoint(banks[n], renamed)
                    o.writestr(n, json.dumps(banks[n], ensure_ascii=False))
                else:
                    o.writestr(n, z.read(n))
    print(f"{out.name}: {sum(len(b) for n, b in banks.items() if 'term_bank' in n)} entries, {len(renamed)}/{len(pics)} pictures shrunk, "
          f"{src.stat().st_size / 1e6:.0f} MB -> {out.stat().st_size / 1e6:.0f} MB", flush=True)
    return out


if __name__ == "__main__":
    # The check: a 1000x500 picture comes out 400 wide, and a bank's path follows its new name.
    big = io.BytesIO()
    Image.new("RGB", (1000, 500), "red").save(big, "PNG")
    assert Image.open(io.BytesIO(shrink(big.getvalue(), 400))).size == (400, 200)
    bank = [["犬", "いぬ", "", "", 0, [{"type": "structured-content", "content": {"tag": "img", "path": "a.png"}}]]]
    repoint(bank, {"a.png": "a.png.webp"})
    assert bank[0][5][0]["content"]["path"] == "a.png.webp" and "width" not in bank[0][5][0]["content"]
    gaiji = {"tag": "img", "path": "media/res/font/wa54b.png"}
    repoint(gaiji, {})
    assert gaiji["width"] == gaiji["height"] == 1 and gaiji["sizeUnits"] == "em"
    assert pictured(bank[0]) and not pictured(["鳳", "", "", "", 0, [{"tag": "img", "path": "res/font/wa54b.png"}]])

    ap = argparse.ArgumentParser()
    ap.add_argument("zips", type=Path, nargs="+")
    ap.add_argument("--side", type=int, default=400, help="longest side in pixels (a 10em picture on the tablet is ~400)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--pictured", action="store_true", help="only the entries with a picture")
    a = ap.parse_args()
    for zp in a.zips:
        build(zp, a.side, a.out, a.pictured)
