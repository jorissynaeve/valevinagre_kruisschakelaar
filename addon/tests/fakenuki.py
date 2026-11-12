"""Een nagebootste Nuki Web API: `GET /smartlock` (de lijst), `GET /smartlock/<id>` en `POST /smartlock/<id>/action` met `{"action": N}`. De sloten hebben de vorm van wat de
echte API antwoordde bij het uitlezen van een Smart Lock 4 Pro (`type` 4, `state` met `batteryCharge`, `doorState` 0 zonder deursensor), zonder ids of namen van iemand.

Wat de echte API doet en de add-on moet verdragen: een opdracht is asynchroon (204 betekent aangenomen, niet uitgevoerd; het slot heeft seconden nodig, of voert hem
nooit uit als het onbereikbaar is), en er is een limiet van verzoeken per minuut (429).
"""
import asyncio
import json

from aiohttp import web

TOKEN = "t" * 40
STATE_AFTER = {1: 3, 2: 1, 3: 5, 4: 6, 5: 5}  # actie -> stand die het slot daarna meldt: unlock 3, lock 1, unlatch 5, lock 'n' go 6, unlatch met lock 'n' go 5


def lock(lid=100000001, name="Voordeur", state=1, battery=80, critical=False, door=0):
    return {"smartlockId": lid, "accountId": 1, "type": 4, "lmType": 0, "name": name, "favorite": False,
            "config": {"name": name, "capabilities": 3, "autoUnlatch": False, "wifiEnabled": True},
            "state": {"mode": 2, "state": state, "trigger": 1, "lastAction": 1, "batteryCritical": critical, "batteryCharging": False, "batteryCharge": battery,
                      "keypadBatteryCritical": False, "doorsensorBatteryCritical": False, "doorState": door, "ringToOpenTimer": 0, "nightMode": False},
            "firmwareVersion": 264709, "serverState": 4}


class FakeNuki:
    def __init__(self, token=TOKEN):
        self.token = token
        self.locks: dict[int, dict] = {}
        self.calls: list[tuple[str, str, object]] = []
        self.lists = 0  # aantal keer dat de hele lijst is opgehaald
        self.status_next = 0  # zoveel opdrachten krijgen deze HTTP-status
        self.status_code = 429
        self.list_status = 0  # de lijst antwoordt met deze status
        self.unreachable = False  # opdrachten worden aangenomen (204) maar het slot voert ze nooit uit
        self.delay = 0.0  # s tot het slot de opdracht uitvoert
        self.runner = None
        self.port = 0

    def add(self, **kw):
        lk = lock(**kw)
        self.locks[lk["smartlockId"]] = lk
        return lk

    def set_state(self, lid, **fields):
        """Het slot verandert (iemand draait aan de knop): de volgende lijst meldt het."""
        self.locks[lid]["state"].update(fields)

    def _unauthorised(self, r: web.Request) -> bool:
        return r.headers.get("Authorization") != f"Bearer {self.token}"

    async def _list(self, r: web.Request) -> web.Response:
        if self._unauthorised(r):
            return web.json_response({"detailMessage": "Unauthorized"}, status=401)
        self.lists += 1
        if self.list_status:
            return web.json_response({}, status=self.list_status)
        return web.json_response(list(self.locks.values()))

    async def _one(self, r: web.Request) -> web.Response:
        if self._unauthorised(r):
            return web.json_response({}, status=401)
        lk = self.locks.get(int(r.match_info["id"]))
        return web.json_response(lk) if lk else web.json_response({}, status=404)

    def _apply(self, lid: int, action: int):
        self.locks[lid]["state"]["state"] = STATE_AFTER[action]

    async def _action(self, r: web.Request) -> web.Response:
        if self._unauthorised(r):
            return web.json_response({}, status=401)
        body = await r.json()
        self.calls.append(("POST", r.path, body))
        lid = int(r.match_info["id"])
        if lid not in self.locks:
            return web.json_response({}, status=404)
        if self.status_next:
            self.status_next -= 1
            return web.json_response({"detailMessage": "nope"} if self.status_code == 400 else {}, status=self.status_code)
        if body.get("action") not in STATE_AFTER:
            return web.json_response({"detailMessage": "invalid action"}, status=400)
        if not self.unreachable:
            if self.delay:
                asyncio.get_running_loop().call_later(self.delay, self._apply, lid, body["action"])
            else:
                self._apply(lid, body["action"])
        return web.Response(status=204)

    async def start(self, port=0):
        app = web.Application()
        app.router.add_get("/smartlock", self._list)
        app.router.add_get("/smartlock/{id}", self._one)
        app.router.add_post("/smartlock/{id}/action", self._action)
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


def house() -> FakeNuki:
    f = FakeNuki()
    f.add(lid=100000001, name="Voordeur", state=1, battery=80)
    f.add(lid=100000002, name="Garage", state=3, battery=14, critical=True, door=2)  # een slot met een deursensor en een batterij die op is
    return f
