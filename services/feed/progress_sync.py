"""Each device's kumapie progress (kumapie_plan.md "own Anki", 4e): where episodes were left, scenes seen, watch time,
lookups and marks, as offline/Progress.kt keeps them. Until 2026-11-16 they rode one kuma3 note per device (AnkiWeb).

POST /api/tv/devices_progress {"person", "device", "data": {...}}: keeps this device's data (written only by it) and
answers {"devices": {device: data}} with every other device of the person; each device merges them itself.
Kept in state/feed/progress/<person>.json.
"""
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root
from dojo import cfg  # noqa: E402
import sync  # noqa: E402

DIR = cfg.state / "feed" / "progress"
MAX_BYTES = 5_000_000  # one device's data; Pedro's tablet had ~0.3 MB on 2026-11-16
_lock = threading.Lock()


def handle(body: dict, folder: Path = DIR, people=None) -> dict:
    """Stores the device's data, answers the other devices'. ValueError when the request isn't right."""
    person, device, data = body.get("person"), body.get("device"), body.get("data")
    if not isinstance(person, str) or person not in (sync.PEOPLE if people is None else people):
        raise ValueError("unknown person")
    if not isinstance(device, str) or not device or len(device) > 64 or device == "pc":
        raise ValueError("a device name is needed")
    if not isinstance(data, dict) or len(json.dumps(data)) > MAX_BYTES:
        raise ValueError("data must be an object")
    f = folder / f"{person}.json"
    with _lock:
        all_ = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        if all_.get(device) != data:
            all_[device] = data
            folder.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".part")
            tmp.write_text(json.dumps(all_, ensure_ascii=False), encoding="utf-8")
            tmp.replace(f)
    return {"devices": {d: v for d, v in all_.items() if d != device}}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        go = lambda dev, data: handle({"person": "p", "device": dev, "data": data}, Path(d), {"p": {}})["devices"]  # noqa: E731
        assert go("tab", {"pos": {"e1": 10}}) == {}
        assert go("pixel", {"pos": {"e2": 5}}) == {"tab": {"pos": {"e1": 10}}}
        assert go("tab", {"pos": {"e1": 20}}) == {"pixel": {"pos": {"e2": 5}}}  # a device's own data is replaced
        assert go("pixel", {"pos": {"e2": 5}}) == {"tab": {"pos": {"e1": 20}}}
        for bad in ({"person": "q", "device": "x", "data": {}}, {"person": "p", "device": "", "data": {}},
                    {"person": "p", "device": "pc", "data": {}}, {"person": "p", "device": "x", "data": []}):
            try:
                handle(bad, Path(d), {"p": {}})
                raise AssertionError(f"accepted {bad}")
            except ValueError:
                pass
    print("progress_sync ok")
