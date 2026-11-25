"""Announce dojo-feed on the LAN as _kumapiepc._tcp (DNS-SD) so the kumapie app finds the PC with NsdManager.
Never raises: no network or a busy port 5353 only logs a warning.  Check: `python announce.py`."""
import socket

TYPE = "_kumapiepc._tcp.local."


def lan_ip() -> str:
    """IPv4 of the default route's interface (not Tailscale 100.x, not virtual adapters)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]


def announce(port: int, log=print):
    """Register the service; returns a callable that unregisters it, or None on failure."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
        ip = lan_ip()
        zc = Zeroconf(interfaces=[ip])
        name = socket.gethostname()
        info = ServiceInfo(TYPE, f"{name}.{TYPE}", port=port, addresses=[socket.inet_aton(ip)],
                           properties={"v": "1"}, server=f"{name}.local.")
        zc.register_service(info)
    except Exception as e:
        log(f"mDNS announce failed (dojo-feed keeps running): {e!r}")
        return None
    log(f"mDNS: {name}.{TYPE} on {ip}:{port}")

    def stop():
        try:
            zc.unregister_service(info)
            zc.close()
        except Exception as e:
            log(f"mDNS unregister failed: {e!r}")
    return stop


if __name__ == "__main__":
    import threading
    from zeroconf import ServiceBrowser, Zeroconf
    seen = threading.Event()

    class L:
        def add_service(self, zc, t, n):
            if n == f"{socket.gethostname()}.{TYPE}":
                i = zc.get_service_info(t, n)
                print("saw", n, [socket.inet_ntoa(a) for a in i.addresses], i.port, i.properties)
                assert i.port == 18769 and i.properties.get(b"v") == b"1"
                seen.set()
        update_service = remove_service = lambda *a: None

    stop = announce(18769)
    assert stop, "announce failed"
    zc = Zeroconf(interfaces=[lan_ip()])
    ServiceBrowser(zc, TYPE, L())
    ok = seen.wait(10)
    zc.close()
    stop()
    assert ok, "did not see itself"
    print("OK")
