"""Een LG-tv met webOS rechtstreeks over het LAN, zonder cloud en zonder de HA-integratie.

De tv luistert op een websocket (SSAP): `wss://<tv>:3001` met een eigen certificaat, op oudere software `ws://<tv>:3000`.
Elk bericht is JSON `{"type", "id", "uri", "payload"}`. De eerste keer vraagt `register` om een koppeling: de tv toont een
melding die je met de afstandsbediening accepteert en geeft dan een `client-key`, die we bewaren. Daarna volstaat die sleutel.

Wat we gebruiken (uri's staan met `ssap://` ervoor):
  com.webos.applicationManager/getForegroundAppInfo   abonnement: welke app vooraan staat (appId)
  com.webos.service.tvpower/power/getPowerState        abonnement: Active, Active Standby, Screen Off, ...
  audio/getVolume                                      abonnement: volume en mute
  system/turnOff, com.webos.service.tvpower/power/turnOffScreen|turnOnScreen
  system.launcher/open {target: url}, system.launcher/launch {id}
Een tv die uit staat of in diepe stand-by is, luistert niet meer: aanzetten gaat met Wake-on-LAN (een UDP-pakket met het MAC-adres).

Getest tegen een echte 85QNED93A6A (webOS 25): koppelen, standen lezen, scherm uit/aan, een URL openen, een app starten en een toast.
Nog niet getest: de tv uitzetten (system/turnOff) en aanzetten met Wake-on-LAN.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from typing import Callable

import aiohttp

log = logging.getLogger("kk.lg")

PORTS = (("wss", 3001), ("ws", 3000))  # nieuwere webOS eerst, oudere als terugval
MAC_RE = re.compile(r"^[0-9a-f]{12}$")
ANSWER = 5.0  # s: zo lang wachten op een antwoord
CONNECT = 10.0  # s: zo lang wachten op een verbinding

URI = "ssap://"
FOREGROUND = URI + "com.webos.applicationManager/getForegroundAppInfo"
POWER_STATE = URI + "com.webos.service.tvpower/power/getPowerState"
VOLUME = URI + "audio/getVolume"
TURN_OFF = URI + "system/turnOff"
SCREEN_OFF = URI + "com.webos.service.tvpower/power/turnOffScreen"
SCREEN_ON = URI + "com.webos.service.tvpower/power/turnOnScreen"
OPEN = URI + "system.launcher/open"
LAUNCH = URI + "system.launcher/launch"
TOAST = URI + "system.notifications/createToast"

# Het manifest zonder handtekening. LG's eigen voorbeeldmanifest (com.lge.test, met handtekening) wordt door een tv met webOS 25 geweigerd
# ("403 Pairing rejected: blacklisted certificate detected"), dus we vragen onze rechten zelf aan.
PERMISSIONS = ["LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE", "TEST_OPEN", "TEST_PROTECTED", "CONTROL_AUDIO", "CONTROL_DISPLAY",
               "CONTROL_INPUT_JOYSTICK", "CONTROL_INPUT_MEDIA_RECORDING", "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_TV",
               "CONTROL_POWER", "CONTROL_TV_SCREEN", "READ_APP_STATUS", "READ_CURRENT_CHANNEL", "READ_INPUT_DEVICE_LIST", "READ_NETWORK_STATE",
               "READ_RUNNING_APPS", "READ_TV_CHANNEL_LIST", "WRITE_NOTIFICATION_TOAST", "READ_POWER_STATE", "READ_COUNTRY_INFO"]
MANIFEST = {"manifestVersion": 1, "appVersion": "1.1", "permissions": PERMISSIONS}


class LgError(Exception):
    pass


def norm_mac(v: str) -> str:
    """"AA:BB:CC:DD:EE:FF" of "aabbccddeeff" -> "aabbccddeeff"; leeg blijft leeg."""
    return re.sub(r"[^0-9a-f]", "", str(v or "").lower())


def magic_packet(mac: str) -> bytes:
    """Wake-on-LAN: 6 x 0xFF en dan 16 x het MAC-adres."""
    m = norm_mac(mac)
    if not MAC_RE.match(m):
        raise LgError("een MAC-adres is 12 hexadecimale tekens, bv. c8f742a1b2c3")
    return b"\xff" * 6 + bytes.fromhex(m) * 16


def wake(mac: str, target: str = "255.255.255.255", port: int = 9) -> bool:
    """Stuurt het pakket; True als het de deur uit ging (niet dat de tv wakker wordt)."""
    pkt = magic_packet(mac)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(pkt, (target, port))
        return True
    except OSError as e:
        log.warning("Wake-on-LAN naar %s mislukt: %s", mac, e)
        return False


def power_of(payload: dict) -> str | None:
    """De stand uit een antwoord van getPowerState. Oudere software meldt `state`, nieuwere ook `processing`."""
    s = payload.get("state")
    return s if isinstance(s, str) else None


def volume_of(payload: dict) -> dict:
    """Volume en mute: nieuwere software zet ze in `volumeStatus`, oudere in het antwoord zelf."""
    p = payload.get("volumeStatus") if isinstance(payload.get("volumeStatus"), dict) else payload
    out = {}
    if isinstance(p.get("volume"), int) and not isinstance(p.get("volume"), bool):
        out["volume"] = p["volume"]
    if isinstance(p.get("muteStatus", p.get("mute")), bool):
        out["muted"] = p.get("muteStatus", p.get("mute"))
    return out


class LgClient:
    """Eén tv: verbindt, koppelt als er nog geen sleutel is, volgt de stand en stuurt bevelen. Bij een verbroken verbinding probeert hij het opnieuw."""

    def __init__(self, host: str, key: str = "", port: int | None = None, scheme: str | None = None,
                 on_state: Callable[[dict], None] | None = None, on_link: Callable[[bool], None] | None = None,
                 on_key: Callable[[str], None] | None = None, answer: float = ANSWER,
                 retry: tuple[float, float] = (5.0, 60.0)) -> None:
        self.host, self.key = host, key
        self.targets = [(scheme, port or dict(PORTS).get(scheme, 3001))] if scheme else [(s, port or p) for s, p in PORTS]
        self.on_state, self.on_link, self.on_key = on_state, on_link, on_key
        self.answer, self.retry = answer, retry
        self.connected = False
        self.pairing = False  # de tv wacht op een druk op OK
        self.rejected = False  # de gebruiker weigerde op de tv: niet blijven aandringen, de run stopt
        self.last_error = ""
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._n = 0
        self._wait: dict[str, asyncio.Future] = {}
        self._subs: dict[str, Callable[[dict], None]] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- verbinding

    async def run(self) -> None:
        delay = self.retry[0]
        async with aiohttp.ClientSession() as http:
            while True:
                good = False
                try:
                    good = await self._connect(http)
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError, LgError, ValueError) as e:
                    self.last_error = str(e) or type(e).__name__
                self._down()
                if self.rejected:
                    return
                delay = self.retry[0] if good else min(delay * 2, self.retry[1])
                await asyncio.sleep(delay)

    async def _connect(self, http: aiohttp.ClientSession) -> bool:
        """Probeert de poorten na elkaar. Geeft True als er een sessie was (ook al viel ze weg): dan wachten we niet langer dan nodig."""
        err = ""
        for scheme, port in self.targets:
            try:
                ws = await asyncio.wait_for(http.ws_connect(f"{scheme}://{self.host}:{port}", ssl=False, heartbeat=20), CONNECT)
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                err = f"{scheme}://{self.host}:{port}: {e}" if str(e) else f"{scheme}://{self.host}:{port}: geen verbinding"
                continue
            self.targets = [(scheme, port)]  # de poort die werkte, onthouden
            await self._session(ws)
            return True
        self.last_error = err or "geen verbinding"
        return False

    async def _session(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        reg = {"forcePairing": False, "pairingType": "PROMPT", "manifest": MANIFEST}
        if self.key:
            reg["client-key"] = self.key
        await self._write({"type": "register", "id": "register_0", "payload": reg})
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._received(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            await ws.close()

    def _down(self) -> None:
        was = self.connected
        self.connected = self.pairing = False
        self._ws = None
        for f in self._wait.values():
            if not f.done():
                f.cancel()
        self._wait.clear()
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        if was and self.on_link:
            self.on_link(False)

    async def _write(self, obj: dict) -> None:
        if self._ws is None or self._ws.closed:
            raise LgError("geen verbinding met de tv")
        await self._ws.send_str(json.dumps(obj))

    # -- ontvangen

    def _received(self, raw: str) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if not isinstance(m, dict):
            return
        mid, kind, payload = m.get("id"), m.get("type"), m.get("payload") if isinstance(m.get("payload"), dict) else {}
        if mid == "register_0":
            self._registered(kind, payload, m)
            return
        if mid in self._subs and kind == "response":
            self._subs[mid](payload)
        f = self._wait.get(mid)
        if f and not f.done():
            if kind == "error":
                f.set_exception(LgError(str(m.get("error") or "fout")))
            else:
                f.set_result(payload)

    def _registered(self, kind: str, payload: dict, m: dict) -> None:
        if kind == "response" and payload.get("pairingType") == "PROMPT":
            self.pairing = True  # de melding staat op de tv
            if self.on_link:
                self.on_link(False)
            return
        if kind == "error":
            self.last_error = str(m.get("error") or "koppeling geweigerd")
            self.rejected = self.pairing or "403" in self.last_error  # een melding die geweigerd of niet beantwoord is
            if self._ws:
                self._spawn(self._ws.close())
            return
        if kind != "registered":
            return
        key = payload.get("client-key")
        if isinstance(key, str) and key and key != self.key:
            self.key = key
            if self.on_key:
                self.on_key(key)
        self.pairing, self.connected, self.last_error = False, True, ""
        self._spawn(self._subscribe())
        if self.on_link:
            self.on_link(True)

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # -- vragen

    async def request(self, uri: str, payload: dict | None = None) -> dict:
        self._n += 1
        mid = f"r{self._n}"
        f = asyncio.get_running_loop().create_future()
        self._wait[mid] = f
        try:
            await self._write({"type": "request", "id": mid, "uri": uri, **({"payload": payload} if payload is not None else {})})
            return await asyncio.wait_for(f, self.answer)
        except asyncio.TimeoutError:
            raise LgError("de tv antwoordt niet") from None
        finally:
            self._wait.pop(mid, None)

    def send(self, uri: str, payload: dict | None = None) -> bool:
        """Een bevel zonder te wachten. True als de tv het kan ontvangen; een fout komt in `last_error`."""
        if not self.connected:
            return False

        async def go() -> None:
            try:
                await self.request(uri, payload)
            except LgError as e:
                self.last_error = f"{uri.removeprefix(URI)}: {e}"
                log.info("%s: %s", self.host, self.last_error)
        self._spawn(go())
        return True

    async def _subscribe(self) -> None:
        for uri, read in ((FOREGROUND, lambda p: {"app": p["appId"]} if isinstance(p.get("appId"), str) else {}),
                          (POWER_STATE, lambda p: {"power": power_of(p)} if power_of(p) else {}),
                          (VOLUME, volume_of)):
            self._n += 1
            mid = f"s{self._n}"
            self._subs[mid] = lambda p, r=read: self._state(r(p))
            try:
                await self._write({"type": "subscribe", "id": mid, "uri": uri})
            except LgError:
                return

    def _state(self, vals: dict) -> None:
        if vals and self.on_state:
            self.on_state(vals)

    # -- bevelen

    def power_off(self) -> bool:
        return self.send(TURN_OFF)

    def screen(self, on: bool) -> bool:
        return self.send(SCREEN_ON if on else SCREEN_OFF)

    def open_url(self, url: str) -> bool:
        return self.send(OPEN, {"target": url})

    def launch(self, app_id: str) -> bool:
        return self.send(LAUNCH, {"id": app_id})

    def toast(self, text: str) -> bool:
        return self.send(TOAST, {"message": text[:200]})
