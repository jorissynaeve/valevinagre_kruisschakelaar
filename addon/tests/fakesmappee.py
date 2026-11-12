"""Een nagebootste Smappee-cloud: het dashboard (`/dashapi/login`) en de API (`/api/v10`, `/api/v11`) zoals `smappee.py` ze aanroept.

Gebouwd naar wat de Home Assistant-integratie `myny-git/smappee_ev` van de echte API leest en schrijft (de vorm van de lijsten en de acties), met andere ids en zonder gegevens van iemand.
Dit is dus een nabootsing van de **aanname** in smappee.py, niet van de echte cloud: wat de echte antwoorden precies bevatten is niet door ons gezien.

Wat de echte cloud doet en de add-on moet verdragen: een onjuist wachtwoord geeft 401, het token verloopt, een opdracht werkt pas later door, te veel verzoeken geven 429 met `Retry-After`.
"""
import asyncio
import secrets
import time

from aiohttp import web

USERNAME, PASSWORD = "jan@example.org", "geheim-wachtwoord"
SITE, LOC, SERIAL, DEVICE, DEVICE2, LED = 7000, 7001, "5001000042", 8101, 8102, 8109
PROP_MIN = "etc.smart.device.type.car.charger.config.min.current"
PROP_MAX = "etc.smart.device.type.car.charger.config.max.current"
NOW_MS = lambda: int(time.time() * 1000)  # noqa: E731


class FakeSmappee:
    def __init__(self, username=USERNAME, password=PASSWORD, connectors=1):
        self.username, self.password = username, password
        self.conn = {DEVICE: self._new_conn(DEVICE, 1, "Laadpunt 1")}
        if connectors == 2:
            self.conn[DEVICE2] = self._new_conn(DEVICE2, 2, "Laadpunt 2")
        self.sessions: list[dict] = []
        self.token_ttl = 3600
        self.tokens: dict[str, float] = {}
        self.logins = 0
        self.bad_logins = 0
        self.lists = 0
        self.session_calls = 0
        self.station_calls = 0
        self.actions: list[tuple[str, int, list]] = []  # (actie, laadpunt, body)
        self.action_status = 200
        self.apply_delay = 0.0
        self.api_status = 0  # de lijst met smart devices antwoordt met deze status
        self.sessions_status = 0
        self.retry_after = ""  # bij 429
        self.maintenance = False
        self.available = True
        self.no_stations = False
        self.runner = None
        self.port = 0

    @staticmethod
    def _new_conn(did: int, pos: int, name: str) -> dict:
        return {"id": did, "pos": pos, "name": name, "uuid": f"uuid-{did}", "state": "IDLE", "connection": "CONNECTED", "iec": "B1", "mode": "NORMAL", "strategy": "NONE",
                "pct": 50, "min": 6, "max": 32, "extra": {}}

    # ------------------------------------------------------------ wat de lader meldt

    def device(self, c: dict) -> dict:
        d = {"id": c["id"], "uuid": c["uuid"], "name": c["name"], "type": {"category": "CARCHARGER"},
             "properties": [{"spec": {"name": "chargingState"}, "value": c["state"]}, {"spec": {"name": "percentageLimit"}, "value": c["pct"]}],
             "configurationProperties": [{"spec": {"name": PROP_MIN}, "values": [{"Quantity": {"value": c["min"], "unit": "A"}}]},
                                         {"spec": {"name": PROP_MAX}, "values": [{"Quantity": {"value": c["max"], "unit": "A"}}]}],
             "carCharger": {"connectionStatus": c["connection"], "iecStatus": c["iec"], "chargingMode": c["mode"], "optimizationStrategy": c["strategy"], "status": {"current": c["state"]}}}
        d.update(c["extra"])
        return d

    def set_state(self, did: int = DEVICE, **kw):
        """De lader meldt iets anders (iemand badgede, trok de stekker eruit, zette hem in de app op Slim)."""
        self.conn[did].update(kw)

    def charging(self, did: int = DEVICE):
        self.set_state(did, state="CHARGING", connection="CONNECTED", iec="C1", mode="NORMAL")

    def add_session(self, start_s: float, end_s: float | None, kwh: float, tag: str | None = "04AABBCC", ms: bool = True, did: int | None = None, **extra):
        k = 1000 if ms else 1
        s = {"from": int(start_s * k), "energy": kwh, **extra}
        if end_s is not None:
            s["to"] = int(end_s * k)
        if tag is not None:
            s["rfidToken"] = tag
        if did is not None:
            s["connectorId"] = did
        self.sessions.append(s)

    # ------------------------------------------------------------ de cloud

    def _auth(self, r: web.Request) -> bool:
        t = r.headers.get("token", "")
        return t in self.tokens and time.monotonic() < self.tokens[t]

    def revoke_tokens(self):
        self.tokens.clear()

    def _gate(self, r: web.Request) -> web.Response | None:
        if self.maintenance:
            return web.Response(status=503, text="The Smappee Dashboard is currently under maintenance.")
        if not self._auth(r):
            return web.Response(status=401)
        if self.retry_after:
            return web.Response(status=429, headers={"Retry-After": self.retry_after})
        return None

    async def _login(self, r: web.Request) -> web.Response:
        self.logins += 1
        if self.maintenance:
            return web.Response(status=503, text="The Smappee Dashboard is currently under maintenance.")
        b = await r.json()
        if b.get("userName") != self.username or b.get("password") != self.password:
            self.bad_logins += 1
            return web.Response(status=401, text="bad credentials")
        tok = secrets.token_hex(24)
        self.tokens[tok] = time.monotonic() + self.token_ttl
        return web.json_response({"token": tok, "refreshToken": secrets.token_hex(16), "tokenExpirationTimestamp": NOW_MS() + self.token_ttl * 1000})

    async def _locations(self, r: web.Request) -> web.Response:
        if (g := self._gate(r)) is not None:
            return g
        site = {"id": SITE, "name": "Thuis", "functionType": "GRID_AND_PV"}
        ctl = {"id": LOC, "parentId": SITE, "name": "Laadpaal", "chargingStation": {"serialNumber": SERIAL}}
        return web.json_response([site] if self.no_stations else [site, ctl])

    async def _station(self, r: web.Request) -> web.Response:
        if (g := self._gate(r)) is not None:
            return g
        self.station_calls += 1
        if r.match_info["serial"] != SERIAL:
            return web.Response(status=404)
        mods = [{"position": c["pos"], "smartDevice": {"id": c["id"], "uuid": c["uuid"], "name": c["name"], "type": {"category": "CARCHARGER"}}} for c in self.conn.values()]
        mods.append({"position": 0, "smartDevice": {"id": LED, "name": "Led", "type": {"category": "LED"}}})
        return web.json_response({"serialNumber": SERIAL, "available": self.available, "modules": mods})

    async def _devices(self, r: web.Request) -> web.Response:
        if (g := self._gate(r)) is not None:
            return g
        self.lists += 1
        if self.api_status:
            return web.Response(status=self.api_status)
        return web.json_response([self.device(c) for c in self.conn.values()] + [{"id": LED, "name": "Led", "type": {"category": "LED"}}])

    async def _sessions(self, r: web.Request) -> web.Response:
        if (g := self._gate(r)) is not None:
            return g
        self.session_calls += 1
        if self.sessions_status:
            return web.Response(status=self.sessions_status)
        return web.json_response(self.sessions)

    def _apply(self, did: int, name: str, body: list):
        c = self.conn[did]
        if name == "pauseCharging":
            c.update(state="PAUSED", mode="PAUSED")
        elif name == "setChargingMode":
            mode = body[0]["values"][0]["String"]
            c.update(state="CHARGING" if c["connection"] == "CONNECTED" else c["state"], mode="NORMAL" if mode == "STANDARD" else mode,
                     strategy="NONE" if mode == "STANDARD" else "EXCESS_ONLY" if mode == "SOLAR" else "SCHEDULES_FIRST_THEN_EXCESS")
        elif name == "setPercentageLimit":
            c["pct"] = body[0]["values"][0]["Integer"]

    async def _action(self, r: web.Request) -> web.Response:
        if (g := self._gate(r)) is not None:
            return g
        did, name = int(r.match_info["did"]), r.match_info["name"]
        body = await r.json()
        self.actions.append((name, did, body))
        if self.action_status != 200:
            return web.json_response({}, status=self.action_status)
        if self.apply_delay:
            asyncio.get_running_loop().call_later(self.apply_delay, self._apply, did, name, body)
        else:
            self._apply(did, name, body)
        return web.json_response({})

    async def start(self, port=0):
        app = web.Application()
        app.router.add_post("/dashapi/login", self._login)
        app.router.add_get("/api/v11/user/servicelocations", self._locations)
        app.router.add_get("/api/v10/chargingstations/{serial}", self._station)
        app.router.add_get("/api/v10/chargingstations/{serial}/sessions", self._sessions)
        app.router.add_get("/api/v10/servicelocation/{loc}/homecontrol/smart/devices", self._devices)
        app.router.add_post("/api/v10/servicelocation/{loc}/homecontrol/smart/devices/{did}/actions/{name}", self._action)
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
