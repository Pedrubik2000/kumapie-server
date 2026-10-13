"""Restart a service when its code changes, without an Administrator.

The dojo services run under NSSM, which restarts a service whenever its process exits (AppExit
Restart, see windows\\services.ps1). So a service only has to exit when one of its files changes:
NSSM starts it again with the new code. Stopping or restarting a service by hand needs an elevated
shell; this does not.

    from dojo.selfrestart import restart_on_change
    restart_on_change(Path(__file__).parent)      # in the service's __main__, before serve_forever()
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]


def _snapshot(paths: list[Path]) -> dict[Path, float]:
    out = {}
    for p in paths:
        files = p.rglob("*.py") if p.is_dir() else [p]
        for f in files:
            if "__pycache__" in f.parts:
                continue
            try:
                out[f] = f.stat().st_mtime
            except OSError:
                pass
    return out


def restart_on_change(*watch: Path, every: float = 5.0, log=print) -> None:
    """Watches the given folders (their .py files) and files, plus the dojo package and config.toml;
    exits the process (code 3) when one changes, is added or removed, so NSSM restarts it."""
    paths = [Path(p) for p in watch] + [CODE / "dojo", CODE / "config.toml"]
    start = _snapshot(paths)

    def loop():
        while True:
            time.sleep(every)
            now = _snapshot(paths)
            if now != start:
                changed = sorted({f.name for f in set(now) ^ set(start)}
                                 | {f.name for f in now if f in start and now[f] != start[f]})
                log(f"code changed ({', '.join(changed)}): exiting so the service restarts with it")
                time.sleep(1)  # a save in several steps: let it finish
                os._exit(3)

    threading.Thread(target=loop, daemon=True, name="restart-on-change").start()
