"""A Yomitan picture dictionary with its pictures shrunk to popup size (素材辞典: 1 GB -> a tenth), for the devices.

  C:\\dojo\\code\\.venv\\Scripts\\python.exe tools\\kumapie\\shrink_dict.py <dictionary.zip>... [--side 400] [--out <folder>]
  (that venv has Pillow; this repo's doesn't)

Every jpg/png/webp becomes a WebP no larger than --side pixels, named <old name>.webp, and the term banks point to
the new names. Other files (gif, svg, avif, css) are copied as they are. Writes "[Pictures] <title>.zip": kumapie
takes the popup's chip from the brackets.
"""
import argparse
import io
import json
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


def repoint(node, renamed: dict):
    if isinstance(node, list):
        for x in node:
            repoint(x, renamed)
    elif isinstance(node, dict):
        if node.get("path") in renamed:
            node["path"] = renamed[node["path"]]
        repoint(node.get("content"), renamed)


def build(src: Path, side: int, out_dir: Path) -> Path:
    with zipfile.ZipFile(src) as z:
        title = json.loads(z.read("index.json"))["title"]
        out = out_dir / f"[Pictures] {safe_name(title)}.zip"
        names = [n for n in z.namelist() if not n.endswith("/")]
        pics = [n for n in names if n.lower().endswith(RASTER)]
        with ProcessPoolExecutor() as pool:
            small = dict(zip(pics, pool.map(shrink, (z.read(n) for n in pics), [side] * len(pics), chunksize=32)))
        renamed = {n: n + ".webp" for n, b in small.items() if b is not None}
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as o:
            for n in names:
                if n in renamed:
                    o.writestr(zipfile.ZipInfo(renamed[n]), small[n])  # stored: WebP doesn't deflate
                elif n.endswith(".json") and "_bank_" in n:
                    bank = json.loads(z.read(n))
                    repoint(bank, renamed)
                    o.writestr(n, json.dumps(bank, ensure_ascii=False))
                else:
                    o.writestr(n, z.read(n))
    print(f"{out.name}: {len(renamed)}/{len(pics)} pictures shrunk, "
          f"{src.stat().st_size / 1e6:.0f} MB -> {out.stat().st_size / 1e6:.0f} MB", flush=True)
    return out


if __name__ == "__main__":
    # The check: a 1000x500 picture comes out 400 wide, and a bank's path follows its new name.
    big = io.BytesIO()
    Image.new("RGB", (1000, 500), "red").save(big, "PNG")
    assert Image.open(io.BytesIO(shrink(big.getvalue(), 400))).size == (400, 200)
    bank = [["犬", "いぬ", "", "", 0, [{"type": "structured-content", "content": {"tag": "img", "path": "a.png"}}]]]
    repoint(bank, {"a.png": "a.png.webp"})
    assert bank[0][5][0]["content"]["path"] == "a.png.webp"

    ap = argparse.ArgumentParser()
    ap.add_argument("zips", type=Path, nargs="+")
    ap.add_argument("--side", type=int, default=400, help="longest side in pixels (a 10em picture on the tablet is ~400)")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    for zp in a.zips:
        build(zp, a.side, a.out)
