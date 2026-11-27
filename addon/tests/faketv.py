"""Een nagebootste LG-tv met webOS: de SSAP-websocket op een lokale poort (zonder TLS), met koppelen, abonnementen en bevelen.

Wat een echte tv doet en de add-on moet verdragen: de eerste keer vraagt `register` om een melding op de tv die je accepteert
(`pairing`: de tv antwoordt eerst `pairingType: PROMPT`), een tv die uit staat luistert niet meer (`stop`), een bevel dat de tv niet kent
komt terug als `type: error` (`errors`) en een abonnement stuurt bij elke wijziging een nieuw bericht.
"""
import asyncio
import json

from aiohttp import web

KEY = "0123456789abcdef0123456789abcdef"
FOREGROUND = "ssap://com.webos.applicationManager/getForegroundAppInfo"
POWER = "ssap://com.webos.service.tvpower/power/getPowerState"
VOLUME = "ssap://audio/getVolume"
POINTER = "ssap://com.webos.service.networkinput/getPointerInputSocket"


class FakeTv:
    def __init__(self, key=KEY, pairing=False):
        self.key = key
        self.pairing = pairing  # de tv wil eerst dat de gebruiker de melding accepteert
        self.accepted = asyncio.Event()
        self.reject = False
        self.state = {"app": "com.webos.app.home", "power": "Active", "volume": 12, "muted": False}
        self.requests: list[tuple[str, dict | None]] = []
        self.registers: list[dict] = []
        self.pointer: list[str] = []  # wat de aanwijzer-socket ontving ("type:click\n\n")
        self.errors: dict[str, str] = {}  # uri -> foutmelding
        self.subs: list[tuple[web.WebSocketResponse, str, str]] = []  # (ws, id, uri)
        self.sockets: list[web.WebSocketResponse] = []
        self.runner = None
        self.port = 0

    def _payload(self, uri: str) -> dict:
        s = self.state
        if uri == FOREGROUND:
            return {"returnValue": True, "appId": s["app"], "subscribed": True}
        if uri == POWER:
            return {"returnValue": True, "state": s["power"], "subscribed": True}
        return {"returnValue": True, "volumeStatus": {"volume": s["volume"], "muteStatus": s["muted"]}, "subscribed": True}

    async def change(self, **fields):
        """De tv verandert (iemand kiest een andere app): elk abonnement krijgt het."""
        self.state.update(fields)
        for ws, mid, uri in list(self.subs):
            if not ws.closed:
                await ws.send_str(json.dumps({"type": "response", "id": mid, "payload": self._payload(uri)}))

    async def _effect(self, uri: str, payload):
        """Wat de echte tv na een bevel meldt (zo gezien op een 85QNED93A6A): het scherm uit geeft `Screen Off`, een URL opent de browser."""
        if uri.endswith("/turnOffScreen"):
            await self.change(power="Screen Off")
        elif uri.endswith("/turnOnScreen"):
            await self.change(power="Active")
        elif uri.endswith("system.launcher/open"):
            await self.change(app="com.webos.app.browser")
        elif uri.endswith("system.launcher/launch") and isinstance(payload, dict) and payload.get("id"):
            await self.change(app=payload["id"])

    async def _ws(self, r: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(r)
        self.sockets.append(ws)
        async for msg in ws:
            m = json.loads(msg.data)
            kind, mid, uri = m.get("type"), m.get("id"), m.get("uri", "")
            if kind == "register":
                self.registers.append(m["payload"])
                if m["payload"].get("client-key") == self.key:
                    await ws.send_str(json.dumps({"type": "registered", "id": mid, "payload": {"client-key": self.key}}))
                elif self.pairing:
                    await ws.send_str(json.dumps({"type": "response", "id": mid, "payload": {"pairingType": "PROMPT", "returnValue": True}}))
                    await self.accepted.wait()
                    if self.reject:
                        await ws.send_str(json.dumps({"type": "error", "id": mid, "error": "403 User rejected pairing", "payload": ""}))
                        continue
                    await ws.send_str(json.dumps({"type": "registered", "id": mid, "payload": {"client-key": self.key}}))
                else:
                    await ws.send_str(json.dumps({"type": "registered", "id": mid, "payload": {"client-key": self.key}}))
            elif kind == "subscribe":
                self.subs.append((ws, mid, uri))
                await ws.send_str(json.dumps({"type": "response", "id": mid, "payload": self._payload(uri)}))
            elif kind == "request":
                self.requests.append((uri, m.get("payload")))
                if uri in self.errors:
                    await ws.send_str(json.dumps({"type": "error", "id": mid, "error": self.errors[uri], "payload": ""}))
                elif uri == POINTER:  # zoals de echte tv: een tweede websocket voor aanwijzer en toetsen
                    await ws.send_str(json.dumps({"type": "response", "id": mid, "payload": {"returnValue": True, "socketPath": f"ws://127.0.0.1:{self.port}/pointer"}}))
                else:
                    await ws.send_str(json.dumps({"type": "response", "id": mid, "payload": {"returnValue": True}}))
                    await self._effect(uri, m.get("payload"))
        self.subs = [x for x in self.subs if x[0] is not ws]
        return ws

    async def _pointer(self, r: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(r)
        async for msg in ws:
            self.pointer.append(msg.data)
        return ws

    async def start(self, port=0):
        app = web.Application()
        app.router.add_get("/", self._ws)
        app.router.add_get("/pointer", self._pointer)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        """De tv gaat uit: de verbinding valt weg en niets luistert nog."""
        for ws in self.sockets:
            await ws.close()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
