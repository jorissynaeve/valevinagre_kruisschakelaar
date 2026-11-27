"""Een nagebootste Reolink-camera: de HTTP-API `POST /cgi-bin/api.cgi` met een array opdrachten, `Login` met een token, en de standen die de add-on opvraagt
(`GetDevInfo`, `GetMdState`, `GetAiState`, `GetWhiteLed`) en de schijnwerper die hij bedient (`SetWhiteLed`). De vorm van de antwoorden volgt de bibliotheek reolink_aio
en de documentatie van Reolink; er is geen echte camera mee vergeleken.

Wat een echte camera doet en de add-on moet verdragen: een token dat verloopt (`rspCode` -6, "please login first"), een camera zonder schijnwerper die op `GetWhiteLed`
een fout geeft (`code` 1, "ability error"), en een aanmelding die faalt.
"""
import uuid

from aiohttp import web


class FakeReolink:
    def __init__(self, user="addon", password="geheim", led=True, ai=("people", "vehicle")):
        self.user, self.password = user, password
        self.tokens: set[str] = set()
        self.logins = 0
        self.md = 0
        self.alarm = {"people": 0, "vehicle": 0, "dog_cat": 0}
        self.ai = tuple(ai)  # welke soorten `support: 1` melden
        self.led = {"channel": 0, "state": 0, "mode": 1, "bright": 100} if led else None
        self.calls: list[tuple[str, dict]] = []  # (opdracht, param)
        self.down = False  # de camera antwoordt niet
        self.snaps = 0  # aantal opnames dat de camera gaf
        self.snap_params: list[dict] = []  # de parameters van elke opname-aanvraag (er mag nooit een wachtwoord in staan)
        self.snap_body = bytes([255, 216, 255, 224]) + b"JFIF-nep-beeld" * 40 + bytes([255, 217])  # een JPEG begint met FFD8 en eindigt op FFD9
        self.runner = None
        self.port = 0

    def expire(self):
        """Alle tokens verlopen, zoals na een uur of een herstart van de camera."""
        self.tokens.clear()

    def _one(self, c: dict, token_ok: bool) -> dict:
        name, param = c.get("cmd"), c.get("param") or {}
        self.calls.append((name, param))
        if name == "Login":
            u = param.get("User") or {}
            if u.get("userName") == self.user and u.get("password") == self.password:
                t = uuid.uuid4().hex
                self.tokens.add(t)
                self.logins += 1
                return {"cmd": name, "code": 0, "value": {"Token": {"leaseTime": 3600, "name": t}}}
            return {"cmd": name, "code": 1, "error": {"detail": "login failed", "rspCode": -27}}
        if not token_ok:
            return {"cmd": name, "code": 1, "error": {"detail": "please login first", "rspCode": -6}}
        if name == "GetDevInfo":
            return {"cmd": name, "code": 0, "value": {"DevInfo": {"model": "Reolink Duo Series P750", "name": "Tuinhuis cam", "serial": "ABC-123 456", "firmVer": "v3.1.0.2368", "channelNum": 1}}}
        if name == "GetMdState":
            return {"cmd": name, "code": 0, "value": {"state": self.md}}
        if name == "GetAiState":
            v = {"channel": 0}
            for k in ("people", "vehicle", "dog_cat"):
                v[k] = {"alarm_state": self.alarm[k], "support": 1 if k in self.ai else 0}
            return {"cmd": name, "code": 0, "value": v}
        if name == "GetWhiteLed":
            if self.led is None:
                return {"cmd": name, "code": 1, "error": {"detail": "ability error", "rspCode": -9}}
            return {"cmd": name, "code": 0, "value": {"WhiteLed": dict(self.led)}}
        if name == "SetWhiteLed":
            if self.led is None:
                return {"cmd": name, "code": 1, "error": {"detail": "ability error", "rspCode": -9}}
            self.led.update({k: v for k, v in (param.get("WhiteLed") or {}).items() if k in ("state", "mode", "bright")})
            return {"cmd": name, "code": 0, "value": {"rspCode": 200}}
        return {"cmd": name, "code": 1, "error": {"detail": "not support", "rspCode": -9}}

    async def _api(self, r: web.Request) -> web.Response:
        if self.down:
            return web.Response(status=503)
        body = await r.json()
        token_ok = r.query.get("token", "") in self.tokens
        return web.json_response([self._one(c, token_ok) for c in body])

    async def _snap(self, r: web.Request) -> web.Response:
        """`GET api.cgi?cmd=Snap&channel=0&rs=..&token=..` geeft een JPEG; met een verlopen token geeft de camera (met status 200) een JSON-fout."""
        if self.down:
            return web.Response(status=503)
        self.snap_params.append(dict(r.query))
        if r.query.get("cmd") != "Snap":
            return web.Response(status=400, text="alleen Snap")
        if r.query.get("token", "") not in self.tokens:
            return web.json_response([{"cmd": "Snap", "code": 1, "error": {"detail": "please login first", "rspCode": -6}}])
        self.snaps += 1
        return web.Response(body=self.snap_body, content_type="image/jpeg")

    async def start(self, port=0):
        app = web.Application()
        app.router.add_post("/cgi-bin/api.cgi", self._api)
        app.router.add_get("/cgi-bin/api.cgi", self._snap)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
