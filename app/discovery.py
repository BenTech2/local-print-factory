"""Find Ultimaker printers on the local network via mDNS (they advertise _ultimaker._tcp)."""
import socket
import time

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

SERVICE = "_ultimaker._tcp.local."


class _Collector(ServiceListener):
    def __init__(self):
        self.names = set()

    def add_service(self, zc, type_, name):
        self.names.add(name)

    def update_service(self, zc, type_, name):
        self.names.add(name)

    def remove_service(self, zc, type_, name):
        pass


def discover(seconds: float = 4.0):
    zc = Zeroconf()
    try:
        col = _Collector()
        ServiceBrowser(zc, SERVICE, col)
        time.sleep(seconds)
        found = []
        for name in col.names:
            info = zc.get_service_info(SERVICE, name, timeout=2000)
            if not info:
                continue
            addrs = [socket.inet_ntoa(a) for a in info.addresses if len(a) == 4]
            props = {k.decode(errors="replace"): (v or b"").decode(errors="replace")
                     for k, v in (info.properties or {}).items()}
            found.append({
                "name": props.get("name") or name.split(".")[0],
                "host": addrs[0] if addrs else (info.server or "").rstrip("."),
                "machine": props.get("machine"),
                "firmware": props.get("firmware_version"),
            })
        return sorted(found, key=lambda f: f["name"])
    finally:
        zc.close()
