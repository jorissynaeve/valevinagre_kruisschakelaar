"""Het lokale Gree-protocol, zonder cloud. Ook Toyotomi, Sinclair, Cooper&Hunter en andere airco's met een Gree-wifimodule.

Een toestel luistert op UDP 7000. Elk bericht is JSON:
  {"t": "pack", "i": 0|1, "uid": 0, "cid": "app", "tcid": <mac>, "pack": <base64>}   (versie 2 ook "tag")
De inhoud van `pack` is weer JSON, versleuteld:
  v1  AES-128-ECB, opgevuld tot een veelvoud van 16 (PKCS#7)
  v2  AES-128-GCM met een vaste nonce en AAD, de tag apart in "tag" (nieuwere modules)
`i` 1 betekent de algemene sleutel van de versie (zoeken en binden), 0 de sleutel van het toestel. Die geeft het toestel bij
het binden (`bindok`), eenmalig; ze verandert niet tot het toestel opnieuw aan de wifi gekoppeld wordt.
De referentie is greeclimate (de Gree-integratie van Home Assistant); de constanten komen daaruit.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Callable

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    CRYPTO = True
except ImportError:  # armv7: zie tuya.py
    CRYPTO = False
    InvalidTag = ValueError

log = logging.getLogger("kk.gree")

PORT = 7000
VERSIONS = ("1", "2")
GENERIC = {"1": b"a3K8Bx%2r8Y7#xDh", "2": b"{yxAHAY_Lm6pbC/<"}
GCM_NONCE = b"\x54\x40\x78\x44\x49\x67\x5a\x51\x6c\x5e\x63\x13"
GCM_AAD = b"qualcomm-test"
SCAN = b'{"t":"scan"}'
MAC_RE = re.compile(r"^[0-9a-f]{12}$")
# alles wat we bij elke statusvraag opvragen; een toestel dat iets niet kent, laat het gewoon weg
COLS = ["Pow", "Mod", "SetTem", "TemUn", "TemRec", "WdSpd", "Air", "Blo", "Health", "SwhSlp", "Lig", "SwingLfRig", "SwUpDn", "Quiet", "Tur", "SvSt", "StHt", "TemSen", "hid"]

POLL_EVERY = 10.0  # s: een Gree-toestel meldt niets uit zichzelf
ANSWER = 2.0  # s: zo lang wachten op een antwoord
MISSES = 3  # zoveel statusvragen zonder antwoord: niet meer bereikbaar


class GreeError(Exception):
    pass


def norm_mac(v: str) -> str:
    """"AA:BB:CC:DD:EE:FF" of "aabbccddeeff" -> "aabbccddeeff"; leeg blijft leeg."""
    return re.sub(r"[^0-9a-f]", "", str(v or "").lower())


# ------------------------------------------------------------ versleuteling (puur)


def _json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def encrypt(version: str, key: bytes, obj) -> tuple[str, str | None]:
    """JSON -> (pack, tag) in base64. v1 heeft geen tag."""
    data = _json(obj)
    if version == "2":
        out = AESGCM(key).encrypt(GCM_NONCE, data, GCM_AAD)
        return base64.b64encode(out[:-16]).decode(), base64.b64encode(out[-16:]).decode()
    pad = 16 - len(data) % 16
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(data + bytes([pad]) * pad) + enc.finalize()).decode(), None


def decrypt(version: str, key: bytes, pack: str, tag: str | None = None) -> dict:
    """Base64 -> JSON. Met een tag wordt die gecontroleerd (een verkeerde sleutel valt zo op); zonder tag niet, zoals greeclimate."""
    try:
        raw = base64.b64decode(pack, validate=True)
        if version == "2":
            if tag:
                data = AESGCM(key).decrypt(GCM_NONCE, raw + base64.b64decode(tag, validate=True), GCM_AAD)
            else:  # GCM zonder tag is CTR vanaf teller 2
                dec = Cipher(algorithms.AES(key), modes.CTR(GCM_NONCE + b"\x00\x00\x00\x02")).decryptor()
                data = dec.update(raw) + dec.finalize()
        else:
            if not raw or len(raw) % 16:
                raise GreeError("geen veelvoud van 16 bytes")
            dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            data = dec.update(raw) + dec.finalize()
            data = data[: data.rfind(b"}") + 1]  # de opvulling (sommige modules vullen niet volgens PKCS#7)
        j = json.loads(data.decode("utf-8"))
    except (ValueError, InvalidTag, UnicodeDecodeError) as e:
        raise GreeError(f"niet te ontcijferen: {e}") from None
    if not isinstance(j, dict):
        raise GreeError("geen JSON-object")
    return j


def envelope(version: str, key: bytes, mac: str, obj, generic: bool = False) -> bytes:
    pack, tag = encrypt(version, key, obj)
    msg = {"cid": "app", "i": 1 if generic else 0, "t": "pack", "uid": 0, "tcid": mac, "pack": pack}
    if tag:
        msg["tag"] = tag
    return _json(msg)


def open_(data: bytes, key: bytes | None, version: str | None = None) -> tuple[dict, str]:
    """Een antwoord -> (inhoud, versie). Met `i` 1 de algemene sleutel, anders `key`. Zonder gekende versie: wat werkt."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise GreeError("geen JSON") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("pack"), str):
        raise GreeError("geen pack")
    tag = msg.get("tag") if isinstance(msg.get("tag"), str) else None
    tries = [version] if version else (["2", "1"] if tag else ["1", "2"])
    err: GreeError | None = None
    for v in tries:
        k = GENERIC[v] if msg.get("i") == 1 or key is None else key
        try:
            return decrypt(v, k, msg["pack"], tag), v
        except GreeError as e:
            err = e
    raise err or GreeError("niet te ontcijferen")


def bind_request(version: str, mac: str) -> bytes:
    body = {"mac": mac, "t": "bind", "uid": 0}
    if version == "2":
        body = {"cid": mac, **body}
    return envelope(version, GENERIC[version], mac, body, generic=True)


def status_request(version: str, key: bytes, mac: str, cols=COLS) -> bytes:
    return envelope(version, key, mac, {"cols": list(cols), "mac": mac, "t": "status"})


def cmd_request(version: str, key: bytes, mac: str, values: dict[str, int]) -> bytes:
    return envelope(version, key, mac, {"opt": list(values), "p": list(values.values()), "t": "cmd"})


def parse_scan(data: bytes, src: str) -> dict | None:
    """Een antwoord op `scan` -> {"mac", "ip", "name", "brand", "model", "firmware", "version"}, of None als het iets anders is."""
    try:
        inner, version = open_(data, None)
    except GreeError:
        return None
    mac = norm_mac(inner.get("mac") or inner.get("cid") or "")
    if inner.get("t") != "dev" or not MAC_RE.match(mac):
        return None
    return {"mac": mac, "ip": src, "name": str(inner.get("name") or "")[:60], "brand": str(inner.get("brand") or ""), "model": str(inner.get("model") or ""),
            "firmware": str(inner.get("ver") or ""), "version": version}


def values_of(inner: dict) -> dict[str, object]:
    """`dat` (cols/dat) of `res` (opt/p of val) -> {eigenschap: waarde}."""
    if inner.get("t") == "dat":
        names, vals = inner.get("cols"), inner.get("dat")
    elif inner.get("t") == "res":
        names, vals = inner.get("opt"), inner.get("val") if isinstance(inner.get("val"), list) else inner.get("p")
    else:
        return {}
    if not isinstance(names, list) or not isinstance(vals, list):
        return {}
    return {str(n): v for n, v in zip(names, vals) if isinstance(v, (int, str)) and not isinstance(v, bool)}


# ------------------------------------------------------------ zoeken op het LAN


class _Scan(asyncio.DatagramProtocol):
    def __init__(self, on_found: Callable[[dict], None]) -> None:
        self.on_found = on_found

    def datagram_received(self, data: bytes, addr) -> None:
        if data.strip() == SCAN:
            return  # onze eigen vraag (de broadcast komt ook bij onszelf)
        found = parse_scan(data, addr[0])
        if found:
            self.on_found(found)


async def scan_socket(on_found: Callable[[dict], None]) -> asyncio.DatagramTransport:
    """Een eigen poort voor de zoekvraag: de antwoorden komen terug naar de poort die vroeg (UDP 7000 is van Tuya)."""
    t, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: _Scan(on_found), local_addr=("0.0.0.0", 0), allow_broadcast=True)
    return t


def scan(transport: asyncio.DatagramTransport, target: str = "255.255.255.255", port: int = PORT) -> None:
    try:
        transport.sendto(SCAN, (target, port))
    except OSError as e:
        log.debug("zoekvraag niet verstuurd: %s", e)


# ------------------------------------------------------------ één toestel


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, client: "GreeClient") -> None:
        self.client = client

    def datagram_received(self, data: bytes, addr) -> None:
        self.client._received(data)

    def error_received(self, exc) -> None:
        self.client.last_error = f"netwerkfout: {exc}"


class GreeClient:
    """Eén airco: vraagt de stand op, bindt als er nog geen sleutel is en stuurt bevelen. Zonder MAC zoekt hij die eerst op."""

    def __init__(self, host: str, mac: str = "", key: str = "", version: str = "auto", port: int = PORT,
                 on_values: Callable[[dict], None] | None = None, on_link: Callable[[bool], None] | None = None,
                 on_bound: Callable[[str, str, str], None] | None = None, poll: float = POLL_EVERY, answer: float = ANSWER,
                 retry: tuple[float, float] = (5.0, 60.0)) -> None:
        self.host, self.port, self.mac = host, port, norm_mac(mac)
        self.key = key.encode("latin1") if key else b""
        self.version = version if version in VERSIONS else "auto"
        self.on_values, self.on_link, self.on_bound = on_values, on_link, on_bound
        self.poll, self.answer, self.retry = poll, answer, retry
        self.connected = False
        self.last_error = ""
        self.firmware = ""
        self._t: asyncio.DatagramTransport | None = None
        self._wait: dict[str, asyncio.Future] = {}
        self._misses = 0

    # -- ontvangen

    def _received(self, data: bytes) -> None:
        if data.strip() == SCAN:
            return
        try:
            inner, version = open_(data, self.key or None, None if self.version == "auto" else self.version)
        except GreeError as e:
            log.debug("%s: onleesbaar antwoord: %s", self.host, e)
            return
        t = inner.get("t")
        f = self._wait.pop(t, None)
        if f and not f.done():
            f.set_result((inner, version))
        if t in ("dat", "res"):
            vals = values_of(inner)
            if vals and self.on_values:
                self.on_values(vals)

    async def _ask(self, data: bytes, want: str) -> tuple[dict, str] | None:
        if self._t is None:
            return None
        f = asyncio.get_running_loop().create_future()
        self._wait[want] = f
        try:
            self._t.sendto(data)
        except OSError as e:
            self.last_error = f"niet verstuurd: {e}"
            self._wait.pop(want, None)
            return None
        try:
            return await asyncio.wait_for(f, self.answer)
        except asyncio.TimeoutError:
            return None
        finally:
            if self._wait.get(want) is f:
                del self._wait[want]

    # -- stappen

    async def _find_mac(self) -> bool:
        got = await self._ask(SCAN, "dev")
        mac = norm_mac((got[0].get("mac") or got[0].get("cid") or "") if got else "")
        if not MAC_RE.match(mac):
            self.last_error = f"geen antwoord van {self.host} op UDP {self.port}: staat de airco aan en op de wifi?"
            return False
        self.mac = mac
        self.firmware = str(got[0].get("ver") or "")
        return True

    async def _bind(self) -> bool:
        for v in (VERSIONS if self.version == "auto" else (self.version,)):
            got = await self._ask(bind_request(v, self.mac), "bindok")
            if got and isinstance(got[0].get("key"), str) and len(got[0]["key"].encode("latin1", "replace")) == 16:
                self.key, self.version = got[0]["key"].encode("latin1"), v
                log.info("Gree %s gebonden (versie %s)", self.mac, v)
                if self.on_bound:
                    self.on_bound(self.mac, got[0]["key"], v)
                return True
        self.last_error = "binden mislukt: de airco antwoordt niet op de vraag om een sleutel"
        return False

    def _link(self, up: bool) -> None:
        if up != self.connected:
            self.connected = up
            if self.on_link:
                self.on_link(up)

    async def run(self) -> None:
        if not CRYPTO:
            self.last_error = "versleuteling niet beschikbaar op deze processor"
            return
        loop = asyncio.get_running_loop()
        delay = self.retry[0]
        try:
            while True:
                try:
                    if self._t is None:
                        self._t, _ = await loop.create_datagram_endpoint(lambda: _Proto(self), remote_addr=(self.host, self.port))
                    if not self.mac and not await self._find_mac():
                        raise GreeError(self.last_error)
                    if not self.key and not await self._bind():
                        raise GreeError(self.last_error)
                    delay = self.retry[0]
                    while True:
                        started = time.monotonic()
                        got = await self._ask(status_request(self.version if self.version != "auto" else "1", self.key, self.mac), "dat")
                        if got:
                            self._misses = 0
                            self.last_error = ""
                            if self.version == "auto":
                                self.version = got[1]
                            self._link(True)
                        else:
                            self._misses += 1
                            if self._misses >= MISSES:
                                self.last_error = "geen antwoord op de statusvraag (adres of sleutel fout?)"
                                self._link(False)
                        await asyncio.sleep(max(0.0, self.poll - (time.monotonic() - started)))
                except (GreeError, OSError) as e:
                    self.last_error = self.last_error or str(e)
                    self._link(False)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.retry[1])
        finally:
            self._link(False)
            if self._t:
                self._t.close()
                self._t = None

    def set(self, values: dict[str, int]) -> bool:
        """Een bevel. De stand verandert pas als het toestel antwoordt (`res`) of bij de volgende statusvraag."""
        if not (self._t and self.key and self.mac and self.connected):
            return False
        try:
            self._t.sendto(cmd_request(self.version if self.version != "auto" else "1", self.key, self.mac, values))
        except OSError as e:
            self.last_error = f"niet verstuurd: {e}"
            return False
        return True
