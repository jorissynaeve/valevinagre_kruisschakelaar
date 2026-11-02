"""Het lokale Tuya-protocol (3.3, 3.4 en 3.5), zonder cloud. Bovenaan pure functies, onderaan de verbinding met één toestel.

Een toestel luistert op TCP 6668. Beveiliging per versie:
  3.3  frames 55AA met CRC32, inhoud AES-ECB met de local key
  3.4  sessiesleutel, daarna frames 55AA met HMAC-SHA256 en AES-ECB
  3.5  sessiesleutel, daarna frames 6699 met AES-GCM (de kop is de AAD)
Veel toestellen laten maar één of enkele verbindingen toe: de SmartLife-app of LocalTuya kan de plek al bezet houden.
"""
from __future__ import annotations

import asyncio
import binascii
import hashlib
import hmac
import json
import logging
import os
import socket
import struct
import time
from typing import Callable

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    CRYPTO = True
except ImportError:  # armv7: geen kant-en-klare wheel voor musl; de rest van de add-on werkt dan gewoon
    CRYPTO = False
    InvalidTag = ValueError

log = logging.getLogger("kk.tuya")

PORT = 6668
VERSIONS = ("3.5", "3.4", "3.3")

SESS_START, SESS_RESP, SESS_FINISH = 3, 4, 5
CONTROL, STATUS, HEARTBEAT, DP_QUERY, CONTROL_NEW, DP_QUERY_NEW = 7, 8, 9, 0x0A, 0x0D, 0x10
NO_VERSION_HEADER = {DP_QUERY, DP_QUERY_NEW, HEARTBEAT, SESS_START, SESS_RESP, SESS_FINISH}

P55 = b"\x00\x00\x55\xaa"
S55 = b"\x00\x00\xaa\x55"
P66 = b"\x00\x00\x66\x99"
S66 = b"\x00\x00\x99\x66"
HDR55 = 16
HDR66 = 18
MAX_FRAME = 64 * 1024

HEARTBEAT_EVERY = 10.0  # s: toestellen sluiten een stille verbinding na ongeveer 30 s
POLL_EVERY = 30.0  # s: wijzigingen komen vanzelf, dit vangt een gemist bericht op
SILENCE = 25.0  # s: zonder enig antwoord is de verbinding dood
ATTEMPT = 5.0  # s: per stap van het verbinden


class TuyaError(Exception):
    pass


class KeyError_(TuyaError):
    """De sleutel of de versie klopt niet."""


# ------------------------------------------------------------ versleuteling


def _pad(b: bytes) -> bytes:
    n = 16 - len(b) % 16
    return b + bytes([n]) * n


def _unpad(b: bytes) -> bytes:
    n = b[-1] if b else 0
    if not 1 <= n <= 16 or b[-n:] != bytes([n]) * n:
        raise KeyError_("foute opvulling na ontsleutelen")
    return b[:-n]


def ecb_encrypt(key: bytes, data: bytes, pad: bool = True) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(_pad(data) if pad else data) + enc.finalize()


def ecb_decrypt(key: bytes, data: bytes, pad: bool = True) -> bytes:
    if not data or len(data) % 16:
        raise KeyError_("lengte past niet bij AES")
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    out = dec.update(data) + dec.finalize()
    return _unpad(out) if pad else out


def session_key(version: str, key: bytes, local: bytes, remote: bytes) -> bytes:
    mixed = bytes(a ^ b for a, b in zip(local, remote))
    if version == "3.4":
        return ecb_encrypt(key, mixed, pad=False)
    return AESGCM(key).encrypt(local[:12], mixed, None)[:16]


# ------------------------------------------------------------ frames


def pack(version: str, key: bytes, seq: int, cmd: int, payload: bytes, iv: bytes | None = None) -> bytes:
    """`payload` is onversleuteld. Voegt de versiekop toe waar die hoort en versleutelt."""
    head = version.encode() + b"\x00" * 12
    plain = payload if cmd in NO_VERSION_HEADER else head + payload
    if version == "3.5":
        iv = iv or os.urandom(12)
        hdr = P66 + struct.pack(">HIII", 0, seq, cmd, 12 + len(plain) + 16)
        return hdr + iv + AESGCM(key).encrypt(iv, plain, hdr[4:]) + S66
    if version == "3.4":
        body = ecb_encrypt(key, plain)
        data = P55 + struct.pack(">III", seq, cmd, len(body) + 36) + body
        return data + hmac.new(key, data, hashlib.sha256).digest() + S55
    body = ecb_encrypt(key, payload)
    if cmd not in NO_VERSION_HEADER:
        body = head + body
    data = P55 + struct.pack(">III", seq, cmd, len(body) + 8) + body
    return data + struct.pack(">I", binascii.crc32(data) & 0xFFFFFFFF) + S55


def frame_length(buf: bytes) -> int | None:
    """Lengte van het frame vooraan, of None als de kop nog niet binnen is. Fout bij onzin: dan loopt de stroom niet meer gelijk."""
    if len(buf) < 4:
        return None
    if buf[:4] == P55:
        if len(buf) < HDR55:
            return None
        n = struct.unpack(">I", buf[12:16])[0] + HDR55
    elif buf[:4] == P66:
        if len(buf) < HDR66:
            return None
        n = struct.unpack(">I", buf[14:18])[0] + HDR66 + 4
    else:
        raise TuyaError("onbekend frame")
    if n > MAX_FRAME:
        raise TuyaError("frame te groot")
    return n


def unpack(version: str, key: bytes, frame: bytes) -> tuple[int, bytes]:
    """Geeft (commando, onversleutelde inhoud zonder retourcode en versiekop)."""
    if frame[:4] == P66:
        cmd = struct.unpack(">I", frame[10:14])[0]
        iv, rest = frame[HDR66:HDR66 + 12], frame[HDR66 + 12:-4]
        try:
            body = AESGCM(key).decrypt(iv, rest, frame[4:HDR66])
        except InvalidTag:
            raise KeyError_("controle van het bericht mislukt")
    else:
        cmd = struct.unpack(">I", frame[8:12])[0]
        if version == "3.4":
            body, mac = frame[HDR55:-36], frame[-36:-4]
            if not hmac.compare_digest(mac, hmac.new(key, frame[:-36], hashlib.sha256).digest()):
                raise KeyError_("controle van het bericht mislukt")
            if len(body) % 16 == 4:  # retourcode
                body = body[4:]
            body = ecb_decrypt(key, body) if body else b""
        else:
            body, crc = frame[HDR55:-8], struct.unpack(">I", frame[-8:-4])[0]
            if crc != binascii.crc32(frame[:-8]) & 0xFFFFFFFF:
                raise TuyaError("CRC klopt niet")
            if len(body) % 16 in (3, 4):  # retourcode, met of zonder versiekop erachter
                body = body[4:]
            if body[:2] == b"3.":
                body = body[15:]
            if body and body[:1] != b"{":
                body = ecb_decrypt(key, body)
    return cmd, _strip(body)


def _strip(body: bytes) -> bytes:
    if body[:1] != b"{" and body[4:5] == b"{" or body[4:6] == b"3.":
        body = body[4:]  # retourcode
    if body[:2] == b"3.":
        body = body[15:]
    return body


def dps_of(body: bytes) -> dict | None:
    """De datapunten uit een bericht, of None als er geen in staan. 3.4 en hoger: {"data":{"dps":…}}."""
    if body[:1] != b"{":
        return None
    try:
        j = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(j, dict):
        return None
    dps = j.get("dps")
    if dps is None and isinstance(j.get("data"), dict):
        dps = j["data"].get("dps")
    return dps if isinstance(dps, dict) else None


def query_message(version: str, device_id: str, needs_dps: bool, now: int) -> tuple[int, bytes]:
    if version in ("3.4", "3.5"):
        return DP_QUERY_NEW, b"{}"
    if needs_dps:  # een oud soort 3.3-toestel ("data unvalid"): moet weten welke punten
        return CONTROL_NEW, _json({"devId": device_id, "uid": device_id, "t": str(now), "dps": {"1": None}})
    return DP_QUERY, _json({"gwId": device_id, "devId": device_id, "uid": device_id, "t": str(now)})


def control_message(version: str, device_id: str, dps: dict, now: int) -> tuple[int, bytes]:
    if version in ("3.4", "3.5"):
        return CONTROL_NEW, _json({"protocol": 5, "t": now, "data": {"dps": dps}})
    return CONTROL, _json({"devId": device_id, "uid": device_id, "t": str(now), "dps": dps})


def _json(o) -> bytes:
    return json.dumps(o, separators=(",", ":")).encode()


# ------------------------------------------------------------ broadcasts op het LAN

UDP_PORTS = (6666, 6667)  # 6666: oude toestellen, onversleuteld; 6667: 3.3 en hoger, met een vaste sleutel
UDP_KEY = hashlib.md5(b"yGAdlopoPVldABfn").digest()


def parse_broadcast(data: bytes) -> dict | None:
    """{"id", "ip", "version", "product"} uit de broadcast van een toestel, of None als het er geen is."""
    try:
        if frame_length(data) != len(data):
            return None
        plain = data[:4] == P55 and b"{" in (data[HDR55:HDR55 + 1], data[HDR55 + 4:HDR55 + 5])
        if plain:  # 6666: leesbaar, eventueel achter een retourcode
            if struct.unpack(">I", data[-8:-4])[0] != binascii.crc32(data[:-8]) & 0xFFFFFFFF:
                return None
            body = _strip(data[HDR55:-8])
        else:
            _, body = unpack("3.3", UDP_KEY, data)
        j = json.loads(body.rstrip(b"\x00").decode("utf-8"))
    except (TuyaError, ValueError, struct.error, IndexError, InvalidTag):
        return None
    if not isinstance(j, dict) or not isinstance(j.get("gwId"), str) or not isinstance(j.get("ip"), str):
        return None
    return {"id": j["gwId"], "ip": j["ip"], "version": str(j.get("version") or ""), "product": str(j.get("productKey") or "")}


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, on_found: Callable[[dict, str], None]) -> None:
        self.on_found = on_found

    def datagram_received(self, data: bytes, addr) -> None:
        found = parse_broadcast(data)
        if found:
            self.on_found(found, addr[0])


async def listen(on_found: Callable[[dict, str], None], ports=UDP_PORTS) -> list[asyncio.DatagramTransport]:
    """Luistert naar de broadcasts. Een poort die bezet is (LocalTuya zonder reuse_port) slaan we over."""
    loop = asyncio.get_running_loop()
    out = []
    for port in ports:
        try:
            t, _ = await loop.create_datagram_endpoint(lambda: _Listener(on_found), local_addr=("0.0.0.0", port),
                                                       reuse_port=hasattr(socket, "SO_REUSEPORT") or None)
            out.append(t)
        except OSError as e:
            log.warning("UDP %d niet beschikbaar voor het zoeken van Tuya-toestellen: %s", port, e)
    return out


# ------------------------------------------------------------ verbinding


class TuyaClient:
    """Eén toestel: blijft verbonden, vraagt de stand op en meldt wijzigingen. `version` "auto" probeert ze om de beurt."""

    def __init__(self, device_id: str, key: str, host: str, version: str = "auto", port: int = PORT,
                 on_dps: Callable[[dict], None] | None = None, on_link: Callable[[bool], None] | None = None,
                 on_version: Callable[[str], None] | None = None, retry: tuple[float, float] = (2.0, 60.0)) -> None:
        self.device_id, self.key, self.host, self.port = device_id, key.encode("latin1"), host, port
        self.version = version
        self.on_dps, self.on_link, self.on_version = on_dps, on_link, on_version
        self.retry = retry
        self.connected = False
        self.last_error = ""
        self._writer: asyncio.StreamWriter | None = None
        self._active = ""  # de versie van deze verbinding
        self._skey = b""
        self._seq = 1
        self._needs_dps = False

    async def run(self) -> None:
        if not CRYPTO:
            self.last_error = "versleuteling niet beschikbaar op deze processor"
            log.warning("%s: %s", self.host, self.last_error)
            return
        delay = self.retry[0]
        while True:
            for v in VERSIONS if self.version == "auto" else (self.version,):
                try:
                    await self._session(v)
                    delay = self.retry[0]  # een goede verbinding die wegviel
                    break
                except asyncio.CancelledError:
                    raise
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, TuyaError) as e:
                    self.last_error = _reason(e, self.version == "auto")
                    log.debug("%s met versie %s: %s", self.host, v, self.last_error)
                finally:
                    self._down()
            log.info("%s: %s", self.host, self.last_error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.retry[1])

    def _down(self) -> None:
        w, self._writer = self._writer, None
        if w:
            w.close()
        if self.connected:
            self.connected = False
            if self.on_link:
                self.on_link(False)

    def set_dps(self, dps: dict) -> bool:
        """Verstuurt een opdracht. False zonder verbinding. De bevestiging is de statusmelding die erop volgt."""
        if not self.connected or not self._writer:
            return False
        self._send(*control_message(self._active, self.device_id, dps, int(time.time())))
        return True

    def _send(self, cmd: int, payload: bytes) -> None:
        self._writer.write(pack(self._active, self._skey, self._seq, cmd, payload))
        self._seq += 1

    async def _read(self, reader: asyncio.StreamReader, buf: bytearray, timeout: float) -> bytes:
        while True:
            n = frame_length(bytes(buf[:HDR66]))
            if n is not None and len(buf) >= n:
                frame = bytes(buf[:n])
                del buf[:n]
                return frame
            chunk = await asyncio.wait_for(reader.read(4096), timeout)
            if not chunk:
                raise TuyaError("verbinding gesloten door het toestel")
            buf += chunk

    async def _negotiate(self, reader, buf: bytearray, version: str) -> None:
        local = os.urandom(16)
        self._skey = self.key
        self._send(SESS_START, local)
        await self._writer.drain()
        cmd, body = 0, b""
        while cmd != SESS_RESP:
            frame = await self._read(reader, buf, ATTEMPT)
            cmd, body = _raw_session(version, self.key, frame)
        if len(body) < 48:
            raise KeyError_("onvolledig antwoord op de sessievraag")
        remote = body[:16]
        if not hmac.compare_digest(body[16:48], hmac.new(self.key, local, hashlib.sha256).digest()):
            raise KeyError_("toestel bewijst de sleutel niet")
        self._send(SESS_FINISH, hmac.new(self.key, remote, hashlib.sha256).digest())
        await self._writer.drain()
        self._skey = session_key(version, self.key, local, remote)

    async def _session(self, version: str) -> None:
        reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), ATTEMPT)
        buf = bytearray()
        self._active, self._seq, self._skey = version, 1, self.key
        if version != "3.3":
            try:
                await self._negotiate(reader, buf, version)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, TuyaError) as e:
                # een toestel met een andere sleutel of versie hangt gewoon op of zwijgt
                raise KeyError_(f"geen sessie: {_reason(e, True) if not isinstance(e, KeyError_) else e}")
        self._send(*query_message(version, self.device_id, self._needs_dps, int(time.time())))
        await self._writer.drain()
        loop = asyncio.get_running_loop()
        first, last_hb, last_poll, last_rx = True, loop.time(), loop.time(), loop.time()
        while True:
            try:
                frame = await self._read(reader, buf, 1.0 if not first else ATTEMPT)
            except asyncio.TimeoutError:
                if first:
                    raise TuyaError("geen antwoord op de statusvraag")
                frame = None
            now = loop.time()
            if frame:
                last_rx = now
                cmd, body = unpack(version, self._skey, frame)
                if b"data unvalid" in body and not self._needs_dps:
                    self._needs_dps = True
                    self._send(*query_message(version, self.device_id, True, int(time.time())))
                    continue
                dps = dps_of(body)
                if dps is not None or (first and cmd in (DP_QUERY, DP_QUERY_NEW)):
                    if first:
                        first = False
                        self._up(version)
                    if dps and self.on_dps:
                        self.on_dps(dps)
            if not first:
                if now - last_rx > SILENCE:
                    raise TuyaError("toestel antwoordt niet meer")
                if now - last_hb >= HEARTBEAT_EVERY:
                    last_hb = now
                    self._send(HEARTBEAT, _json({"gwId": self.device_id, "devId": self.device_id}))
                if now - last_poll >= POLL_EVERY:
                    last_poll = now
                    self._send(*query_message(version, self.device_id, self._needs_dps, int(time.time())))
                await self._writer.drain()

    def _up(self, version: str) -> None:
        self.connected, self.last_error = True, ""
        log.info("%s: verbonden, protocol %s", self.host, version)
        if self.version != version:
            self.version = version  # gevonden: bij het opnieuw verbinden niet weer zoeken
            if self.on_version:
                self.on_version(version)
        if self.on_link:
            self.on_link(True)


def _raw_session(version: str, key: bytes, frame: bytes) -> tuple[int, bytes]:
    """Het antwoord op de sessievraag: 16 bytes nonce en 32 bytes HMAC, eventueel achter een retourcode."""
    cmd, body = unpack(version, key, frame)
    return cmd, body[-48:] if len(body) in (48, 52) else body


def _reason(e: Exception, searching: bool) -> str:
    if isinstance(e, KeyError_):
        return f"sleutel of protocolversie klopt niet ({e})"
    if isinstance(e, asyncio.TimeoutError):
        return "geen antwoord (bezet door de app of een andere koppeling?)"
    if isinstance(e, asyncio.IncompleteReadError):
        return "verbinding gesloten door het toestel"
    return str(e) or e.__class__.__name__
