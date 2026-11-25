"""Announce dojo-feed on the LAN as _kumapiepc._tcp (DNS-SD) so the kumapie app finds the PC with NsdManager.
Runs in a daemon thread (HTTP never waits for it): retries until it works, re-announces when the LAN IP changes.
Never raises: no network or a busy port 5353 only logs a warning.  Check: `python announce.py`."""
import socket
import threading

TYPE = "_kumapiepc._tcp.local."
RETRY = 60    # s between tries while it fails (no network at boot)
RECHECK = 180  # s between LAN-IP checks while it works


def lan_ip() -> str:
    """IPv4 of the default route's interface (not Tailscale 100.x, not virtual adapters)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]


def _register(ip: str, port: int, instance: str):
    """-> (Zeroconf, ServiceInfo). The SRV target is `<instance>-kumapie.local.`, a name only zeroconf answers for:
    Windows' own mDNS answers for <hostname>.local with Tailscale/WSL/IPv6 addresses and Android takes one."""
    from zeroconf import ServiceInfo, Zeroconf
    zc = Zeroconf(interfaces=[ip])
    try:
        info = ServiceInfo(TYPE, f"{instance}.{TYPE}", port=port, addresses=[socket.inet_aton(ip)],
                           properties={"v": "1"}, server=f"{instance}-kumapie.local.")
        zc.register_service(info)
    except Exception:
        zc.close()
        raise
    return zc, info


def _unregister(zc, info, log):
    try:
        zc.unregister_service(info)
        zc.close()
    except Exception as e:
        log(f"mDNS unregister failed: {e!r}")


def announce(port: int, log=print):
    """Start announcing in the background; returns a callable that stops it and unregisters."""
    done = threading.Event()
    name = socket.gethostname()

    def run():
        cur = None  # (zc, info, ip)
        while not done.is_set():
            wait = RECHECK
            try:
                ip = lan_ip()
                if cur and cur[2] != ip:
                    log(f"mDNS: LAN address changed {cur[2]} -> {ip}")
                    _unregister(cur[0], cur[1], log)
                    cur = None
                if not cur:
                    cur = (*_register(ip, port, name), ip)
                    log(f"mDNS: {name}.{TYPE} on {ip}:{port}")
            except Exception as e:
                log(f"mDNS announce failed, retrying (dojo-feed keeps running): {e!r}")
                if cur:
                    _unregister(cur[0], cur[1], log)
                    cur = None
                wait = RETRY
            done.wait(wait)
        if cur:
            _unregister(cur[0], cur[1], log)

    t = threading.Thread(target=run, daemon=True, name="mdns-announce")
    t.start()

    def stop():
        done.set()
        t.join(10)
    return stop


if __name__ == "__main__":
    from zeroconf import ServiceBrowser, Zeroconf
    inst = f"{socket.gethostname()}-check"  # never the live instance name: no clash, no goodbye for the live one
    full = f"{inst}.{TYPE}"
    seen = threading.Event()
    got = {}

    class L:
        def add_service(self, zc, t, n):
            if n == full:
                i = zc.get_service_info(t, n)
                got["i"] = i
                seen.set()
        update_service = remove_service = lambda *a: None

    ip = lan_ip()
    zc1, info = _register(ip, 18769, inst)
    zc2 = Zeroconf(interfaces=[ip])
    ServiceBrowser(zc2, TYPE, L())
    ok = seen.wait(10)
    zc2.close()
    _unregister(zc1, info, print)
    assert ok, "did not see itself"
    i = got["i"]
    print("saw", full, [socket.inet_ntoa(a) for a in i.addresses], i.port, i.server, i.properties)
    assert i.port == 18769 and i.properties.get(b"v") == b"1" and i.server == f"{inst}-kumapie.local."
    assert [socket.inet_ntoa(a) for a in i.addresses] == [ip]
    print("OK")
