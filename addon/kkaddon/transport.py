"""UDP-link naar de bridges (DESIGN.md §2): elk frame gaat als los datagram naar het broadcast-adres en
naar vaste unicast-peers; ontvangen datagrammen gaan naar de engine. Een frame is een datagram, zonder omhulsel."""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Callable

from .proto import MAX_FRAME

log = logging.getLogger("kk.udp")


def split_hostport(spec: str, default_port: int) -> tuple[str, int]:
    spec = spec.strip()
    if spec.startswith("["):  # [::1]:1234
        host, _, rest = spec[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if spec.count(":") == 1:
        host, port = spec.split(":")
        return host, int(port)
    return spec, default_port


class UdpTransport(asyncio.DatagramProtocol):
    def __init__(self, port: int, broadcast_addr: str, peers: list[str], hub: bool = True) -> None:
        self.port = port
        self.broadcast = (broadcast_addr, port) if broadcast_addr else None
        self.peer_specs = [split_hostport(p, port) for p in peers]
        self.hub = hub
        self.on_frame: Callable[[bytes, tuple], None] = lambda data, addr: None
        self._sock: asyncio.DatagramTransport | None = None
        self._resolved: list[tuple[str, int]] = []
        self._own_ips: set[str] = {"127.0.0.1"}
        self.seen: dict[str, dict] = {}  # ip -> {"last": wall, "frames": n}
        self._err_at = 0.0
        self.tx_count = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("0.0.0.0", self.port))
        s.setblocking(False)
        self._sock, _ = await loop.create_datagram_endpoint(lambda: self, sock=s)
        try:
            self._own_ips |= set(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
        await self._resolve()
        loop.create_task(self._resolve_loop())
        log.info("UDP luistert op :%d, broadcast %s, peers %s", self.port, self.broadcast, self._resolved)

    async def _resolve(self) -> None:
        loop = asyncio.get_running_loop()
        out = []
        for host, port in self.peer_specs:
            try:
                infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
                out.append(infos[0][4][:2])
            except OSError as e:
                log.warning("peer %s niet te vinden: %s", host, e)
        self._resolved = out

    async def _resolve_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self._resolve()

    def close(self) -> None:
        if self._sock:
            self._sock.close()

    # ---- zenden

    def _sendto(self, raw: bytes, addr: tuple) -> None:
        try:
            self._sock.sendto(raw, addr)
        except OSError as e:
            now = time.monotonic()
            if now - self._err_at > 60:  # niet elke 20 ms hetzelfde loggen
                self._err_at = now
                log.warning("zenden naar %s mislukt: %s", addr, e)

    def send(self, raw: bytes) -> None:
        if not self._sock or len(raw) > MAX_FRAME:
            return
        self.tx_count += 1
        if self.broadcast:
            self._sendto(raw, self.broadcast)
        for addr in self._resolved:
            self._sendto(raw, addr)

    def relay(self, raw: bytes, source: tuple | None) -> None:
        """Hub: een frame van de ene unicast-peer ook naar de andere sturen (bv. een bridge die achter
        een repeater zit die broadcast filtert). Broadcast bereikt iedereen al, dus alleen unicast."""
        if not self.hub or not self._sock:
            return
        src_ip = source[0] if source else None
        for addr in self._resolved:
            if addr[0] != src_ip:
                self._sendto(raw, addr)

    # ---- ontvangen

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if addr[0] not in self._own_ips:
            s = self.seen.setdefault(addr[0], {"frames": 0, "last": 0.0})
            s["frames"] += 1
            s["last"] = time.time()
        self.on_frame(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("udp-fout: %s", exc)

    def peers_info(self) -> list[dict]:
        configured = {a[0] for a in self._resolved}
        return [
            {"ip": ip, "frames": v["frames"], "last": v["last"], "configured": ip in configured}
            for ip, v in sorted(self.seen.items())
        ]
