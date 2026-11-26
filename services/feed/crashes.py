r"""POST /api/crash: kumapie apps send a crash report; saved as JSON in <state>\feed\crashes and announced to Pía
in the Oficina (its own /api/send). Check: .venv\Scripts\python.exe services\feed\crashes.py"""
import ipaddress
import json
import re
import threading
import time
import tomllib
import urllib.request
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

MAX_BODY = 256 * 1024
PER_HOUR = 20
FIELDS = ("device", "model", "android", "abi", "app", "version", "kind", "time", "summary", "trace")
DIR = None  # set by server.py (state\feed\crashes)
OFICINA_URL = None  # None = read [oficina] port from the code repo's config.toml at the first report
OFICINA_CONFIG = Path(r"C:\dojo\code\config.toml")
log = print
_recent = defaultdict(deque)  # device -> report times (s)
_lock = threading.Lock()
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def allowed(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_loopback or a.is_private or a in _TAILSCALE


def _oficina_url():
    if OFICINA_URL:
        return OFICINA_URL
    try:
        port = tomllib.loads(OFICINA_CONFIG.read_text(encoding="utf-8"))["oficina"]["port"]
        return f"http://127.0.0.1:{port}/api/send"
    except (OSError, KeyError, ValueError) as e:
        log(f"crash: Oficina port not found ({e}); report saved only")


def _tell_pia(r, path):
    url = _oficina_url()
    if not url:
        return
    text = (f"(kumapie crash report, automatic; not the jefe) {r['model']} · kumapie {r['version']} · "
            f"{r['kind']}: {r['summary']}. File: {path}")
    try:
        req = urllib.request.Request(url, json.dumps({"key": "pia", "text": text}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as e:  # saved anyway
        log(f"crash: could not tell Pía ({e})")


def receive(ip, length, read):
    """-> (status, json). read(n) reads the body; nothing beyond FIELDS is kept or logged."""
    if not allowed(ip):
        return 403, {"error": "forbidden"}
    if length > MAX_BODY:
        return 413, {"error": "too big"}
    try:
        body = json.loads(read(length) or b"{}")
        r = {k: body[k] for k in FIELDS}
        if type(r["time"]) is not int:
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return 400, {"error": "bad report"}
    for k in FIELDS:
        if k != "time":
            r[k] = str(r[k])
    r["summary"] = r["summary"].replace("\n", " ")[:300]
    r["kind"] = r["kind"] if r["kind"] in ("java", "native", "anr") else "other"
    dev = re.sub(r"[^\w.-]", "_", r["device"])[:40] or "unknown"
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        if any(DIR.glob(f"*-{dev}.json")):  # same device + time = same report
            for f in DIR.glob(f"*-{dev}.json"):
                try:
                    if json.loads(f.read_text(encoding="utf-8")).get("time") == r["time"]:
                        return 200, {"dup": True}
                except (OSError, ValueError):
                    pass
        q, now = _recent[dev], time.time()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= PER_HOUR:
            return 429, {"error": "too many reports"}
        q.append(now)
        f = DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{dev}.json"
        n = 1
        while f.exists():
            n += 1
            f = DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{dev}-{n}.json"
        f.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    threading.Thread(target=_tell_pia, args=(r, f), daemon=True).start()
    return 200, {"ok": True, "id": f.name}


if __name__ == "__main__":  # check: a test instance, a fake Oficina; the real one is never touched
    import tempfile
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    got = []

    class Fake(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

        def log_message(self, *a): pass

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    OFICINA_URL = f"http://127.0.0.1:{fake.server_port}/api/send"
    DIR = Path(tempfile.mkdtemp()) / "crashes"
    rep = dict(device="dev1", model="TEST", android="14", abi="arm64", app="kumapie", version="1.0",
               kind="java", time=123, summary="boom", trace="x")
    post = lambda b, ip="127.0.0.1": receive(ip, len(b), lambda n: b)
    st, out = post(json.dumps(rep).encode())
    assert st == 200 and out["ok"] and (DIR / out["id"]).exists(), (st, out)
    assert json.loads((DIR / out["id"]).read_text(encoding="utf-8")) == rep
    for _ in range(50):
        if got: break
        time.sleep(0.1)
    assert got and got[0]["key"] == "pia" and "TEST · kumapie 1.0 · java: boom. File: " in got[0]["text"], got
    assert post(json.dumps(rep).encode()) == (200, {"dup": True})
    assert post(b"x" * (MAX_BODY + 1))[0] == 413
    assert post(b"{}")[0] == 400
    assert post(json.dumps(rep).encode(), "8.8.8.8")[0] == 403
    assert post(json.dumps({**rep, "time": 5}).encode(), "100.101.1.1")[0] == 200
    for i in range(30):
        st = post(json.dumps({**rep, "time": 1000 + i}).encode())[0]
    assert st == 429, st
    print("crashes check ok")
