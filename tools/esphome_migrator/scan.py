"""ESPHome-apparaten zoeken en bevragen, zonder extra pakketten (alleen stdlib; mDNS via zeroconf als dat er is).

Bevragen gebeurt met de native API van ESPHome (poort 6053, ongeversleuteld): een Hello en een DeviceInfo-verzoek.
Dat werkt voor oude en nieuwe versies, want dit deel van het protocol is nooit veranderd. Een toestel met API-versleuteling
(Noise) antwoordt niet in klare tekst: dat melden we als "versleuteld"."""
from __future__ import annotations

import ipaddress
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

API_PORT = 6053


@dataclass
class Device:
    ip: str
    name: str = ""
    mac: str = ""
    version: str = ""
    compiled: str = ""
    model: str = ""
    server_info: str = ""
    api: str = ""
    encrypted: bool = False
    web: bool = False
    error: str = ""
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- protobuf (alleen wat we nodig hebben)

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7


def _fields(buf: bytes) -> dict[int, list]:
    out: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _read_varint(buf, i)
        elif wire == 2:
            ln, i = _read_varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wire == 5:
            v, i = buf[i:i + 4], i + 4
        elif wire == 1:
            v, i = buf[i:i + 8], i + 8
        else:
            break
        out.setdefault(num, []).append(v)
    return out


def _s(f: dict, n: int) -> str:
    v = f.get(n)
    return v[0].decode("utf-8", "replace") if v and isinstance(v[0], bytes) else ""


def _frame(msg_type: int, payload: bytes) -> bytes:
    return b"\x00" + _varint(len(payload)) + _varint(msg_type) + payload


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("verbinding gesloten")
        buf += c
    return buf


def _recv_frame(s: socket.socket) -> tuple[int, bytes]:
    if _recv_exact(s, 1) != b"\x00":
        raise ValueError("encrypted")  # 0x01 = Noise-preambule
    n = shift = 0
    while True:
        b = _recv_exact(s, 1)[0]
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
    t = shift2 = 0
    while True:
        b = _recv_exact(s, 1)[0]
        t |= (b & 0x7F) << shift2
        if not b & 0x80:
            break
        shift2 += 7
    return t, _recv_exact(s, n) if n else b""


def query(ip: str, port: int = API_PORT, timeout: float = 3.0) -> Device:
    """Vraagt naam, mac, ESPHome-versie en bouwtijd op. Gooit nooit: fouten komen in `error`."""
    d = Device(ip)
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        d.error = f"geen antwoord op poort {port} ({e.__class__.__name__})"
        return d
    try:
        s.settimeout(timeout)
        client = b"huisconfig-scan"
        s.sendall(_frame(1, b"\x0a" + _varint(len(client)) + client + b"\x10\x01\x18\x0a"))  # HelloRequest: client_info, api 1.10
        try:
            t, p = _recv_frame(s)
        except ValueError:
            d.encrypted = True
            d.error = "API is versleuteld (Noise): naam en versie zijn niet op te vragen zonder sleutel"
            return d
        if t != 2:
            d.error = f"onverwacht antwoord (type {t})"
            return d
        f = _fields(p)
        d.api = f"{(f.get(1) or [0])[0]}.{(f.get(2) or [0])[0]}"
        d.server_info, d.name = _s(f, 3), _s(f, 4)
        s.sendall(_frame(9, b""))  # DeviceInfoRequest
        t, p = _recv_frame(s)
        if t == 10:
            f = _fields(p)
            d.name = _s(f, 2) or d.name
            d.mac = _s(f, 3)
            d.version = _s(f, 4)
            d.compiled = _s(f, 5)
            d.model = _s(f, 6)
        if not d.version and "esphome v" in d.server_info:
            d.version = d.server_info.split("esphome v", 1)[1].rstrip(") ")
    except (OSError, ConnectionError, IndexError, ValueError) as e:
        d.error = f"bevragen mislukt: {e}"
    finally:
        s.close()
    d.web = _port_open(ip, 80, 0.5)
    return d


def _port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- zoeken

def local_networks() -> list[str]:
    """De /24-netwerken van deze computer, als `192.168.1.0/24`."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # het adres van de standaardroute (verstuurt niets)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    nets = []
    for ip in sorted(ips):
        a = ipaddress.ip_address(ip)
        if a.is_loopback or a.is_link_local:
            continue
        nets.append(str(ipaddress.ip_network(f"{ip}/24", strict=False)))
    return nets


def mdns(seconds: float = 4.0) -> set[str]:
    """IP-adressen die zich als `_esphomelib._tcp` melden. Leeg als zeroconf ontbreekt."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return set()
    found: set[str] = set()
    zc = Zeroconf()

    class L:
        def add_service(self, zc_, type_, name):
            info = zc_.get_service_info(type_, name, timeout=1500)
            if info:
                found.update(socket.inet_ntoa(a) for a in info.addresses if len(a) == 4)

        update_service = remove_service = lambda self, *a: None

    try:
        ServiceBrowser(zc, "_esphomelib._tcp.local.", L())
        threading.Event().wait(seconds)
    finally:
        zc.close()
    return found


def scan(networks: list[str], on_found: Callable[[Device], None], on_progress: Callable[[str], None] = lambda m: None,
         stop: threading.Event | None = None) -> None:
    """Zoekt toestellen via mDNS en een poortscan op 6053 en roept `on_found` per toestel aan."""
    stop = stop or threading.Event()
    seen: set[str] = set()
    lock = threading.Lock()

    def probe(ip: str) -> None:
        with lock:
            if ip in seen or stop.is_set():
                return
            seen.add(ip)
        on_found(query(ip))

    on_progress("mDNS…")
    for ip in sorted(mdns()):
        probe(ip)
    for net in networks:
        hosts = [str(h) for h in ipaddress.ip_network(net, strict=False).hosts()]
        on_progress(f"{net}: {len(hosts)} adressen…")

        def check(ip: str) -> None:
            if not stop.is_set() and _port_open(ip, API_PORT, 0.6):
                probe(ip)

        with ThreadPoolExecutor(max_workers=64) as ex:
            list(ex.map(check, hosts))
    on_progress("klaar")
