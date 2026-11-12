"""Een nagebootst HUISDEV-apparaat (protocol/huisdev/HUISDEV.md 1.0): echte UDP en echte HTTP (aiohttp), gebouwd naar de spec en niet naar de add-on.

Wat de tests ermee kunnen: punten en acties toevoegen en weghalen (de `rev` volgt), waarden zetten, het apparaat uitzetten en weer aan, en
zien welke verzoeken er kwamen. De foutcodes zijn die van HUISDEV §4.
"""
import asyncio
import hashlib
import json

from aiohttp import web


class FakeEsp:
    def __init__(self, hid="pool-a1b2c3", type="poolcontroller", name="Zwembad", fw="0.3.1", proto="1.0", claimed_by_other=False):
        self.hid, self.type, self.name, self.fw, self.proto = hid, type, name, fw, proto
        self.points: dict[str, dict] = {}  # id -> {"def": {...}, "v": waarde of None, "age": ms}
        self.actions: dict[str, dict] = {}  # id -> {"def": {...}}
        self.disabled: dict[str, str] = {}
        self.token = "x" * 16 if claimed_by_other else ""
        self.calls: list[tuple[str, str, dict, str]] = []  # (methode, pad, body, token)
        self.queue_sets = False  # `queued` in plaats van `applied`: de waarde verandert niet
        self.reject: str | None = None  # een set of actie weigeren met deze reden
        self.up = 0
        self.runner = None
        self.http_port = 0
        self.udp = None
        self.udp_port = 0
        self.udp_seen: list[bytes] = []
        self.hits = {"state": 0, "descriptor": 0}

    # ------------------------------------------------------------ wat het apparaat heeft

    def point(self, pid, label, kind, v=None, **kw):
        d = {"id": pid, "label": label, "kind": kind, **kw}
        self.points[pid] = {"def": d, "v": v, "age": 0}
        return self

    def action(self, aid, label, **kw):
        self.actions[aid] = {"def": {"id": aid, "label": label, **kw}}
        return self

    def remove_point(self, pid):
        self.points.pop(pid, None)

    def set_value(self, pid, v, age=0):
        self.points[pid]["v"], self.points[pid]["age"] = v, age

    @property
    def rev(self) -> str:
        body = json.dumps([[p["def"] for p in self.points.values()], [a["def"] for a in self.actions.values()]], sort_keys=True)
        return hashlib.sha1(body.encode()).hexdigest()[:8]

    def descriptor(self) -> dict:
        return {"proto": self.proto, "rev": self.rev, "device": {"id": self.hid, "type": self.type, "name": self.name, "fw": self.fw, "mac": "AA:BB:CC:A1:B2:C3", "lib": "1.0.0"},
                "points": [p["def"] for p in self.points.values()], "actions": [a["def"] for a in self.actions.values()]}

    def state(self) -> dict:
        pts = {}
        for pid, p in self.points.items():
            pts[pid] = {"v": p["v"], "age": p["age"]} if p["v"] is not None else {"v": None}
        return {"proto": self.proto, "rev": self.rev, "up": self.up, "points": pts, "actions": {a: {"disabled": True, "reason": r} for a, r in self.disabled.items()}}

    def discovery(self) -> dict:
        return {"huisdev": self.proto, "id": self.hid, "type": self.type, "name": self.name, "fw": self.fw, "rev": self.rev, "port": self.http_port, "claimed": bool(self.token)}

    # ------------------------------------------------------------ HTTP

    def _err(self, status, code, reason=""):
        return web.json_response({"ok": False, "error": code, "reason": reason}, status=status)

    async def _handle(self, r: web.Request) -> web.Response:
        body = {}
        if r.method == "POST":
            try:
                body = await r.json()
            except ValueError:
                body = {}
        tok = r.headers.get("X-Huisdev-Token", "")
        path = r.path
        self.calls.append((r.method, path, body, tok))
        ok_tok = bool(self.token) and tok == self.token
        if path == "/huisdev/descriptor":
            if r.method != "GET":
                return self._err(405, "method")
            self.hits["descriptor"] += 1
            return web.json_response(self.descriptor())
        if path == "/huisdev/state":
            if r.method != "GET":
                return self._err(405, "method")
            if self.token and not ok_tok:
                return self._err(401, "token")
            self.hits["state"] += 1
            return web.json_response(self.state())
        if r.method != "POST":
            return self._err(405, "method")
        if path == "/huisdev/claim":
            if self.token:
                return self._err(409, "already_claimed")
            t = body.get("token")
            if not isinstance(t, str) or not 16 <= len(t) <= 128:
                return self._err(422, "bad_token", "16-128 tekens")
            self.token = t
            return web.json_response({"ok": True})
        if path == "/huisdev/unclaim":
            if not self.token:
                return self._err(409, "not_claimed")
            if not ok_tok:
                return self._err(401, "token")
            self.token = ""
            return web.json_response({"ok": True})
        if path in ("/huisdev/set", "/huisdev/do"):
            if not self.token:
                return self._err(403, "not_claimed", "eerst claimen")
            if not ok_tok:
                return self._err(401, "token")
            return await (self._set(body) if path.endswith("set") else self._do(body))
        return self._err(404, "unknown_path")

    def _check(self, d: dict, value, key="value"):
        """Controleert een waarde zoals de spec dat van het apparaat eist. Geeft de foutcode, of None."""
        k = d["kind"]
        if k == "text":
            return None if isinstance(value, str) else "bad_value"
        if k == "bool":
            return None if isinstance(value, bool) else "bad_value"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "bad_value"
        if k in ("int", "enum") and value != int(value):
            return "bad_value"
        if k == "enum":
            return None if value in [o["v"] for o in d.get("options", [])] else "bad_option"
        if "min" in d and value < d["min"] or "max" in d and value > d["max"]:
            return "range"
        if d.get("step"):
            q = (value - d.get("min", 0)) / d["step"]
            if abs(q - round(q)) > 1e-6:
                return "step"
        return None

    async def _set(self, body):
        p = self.points.get(body.get("id"))
        if not p:
            return self._err(404, "unknown_point")
        d = p["def"]
        if not d.get("writable"):
            return self._err(403, "not_writable")
        if d.get("danger") and body.get("confirm") is not True:
            return self._err(409, "confirm_required", d.get("confirm", ""))
        if "value" not in body:
            return self._err(422, "missing_value")
        e = self._check(d, body["value"])
        if e:
            return self._err(422, e)
        if self.reject:
            return web.json_response({"ok": False, "result": "rejected", "reason": self.reject})
        if self.queue_sets:
            return web.json_response({"ok": True, "result": "queued", "reason": ""})
        p["v"], p["age"] = body["value"], 0
        return web.json_response({"ok": True, "result": "applied", "reason": ""})

    async def _do(self, body):
        a = self.actions.get(body.get("id"))
        if not a:
            return self._err(404, "unknown_action")
        d = a["def"]
        if body.get("id") in self.disabled:
            return self._err(409, "disabled", self.disabled[body["id"]])
        if d.get("danger") and body.get("confirm") is not True:
            return self._err(409, "confirm_required", d.get("confirm", ""))
        args = body.get("args") or {}
        for arg in d.get("args", []):
            if arg["id"] not in args:
                return self._err(422, "missing_arg", arg["id"])
            e = self._check(arg, args[arg["id"]])
            if e:
                return self._err(422, e, arg["id"])
        if self.reject:
            return web.json_response({"ok": False, "result": "rejected", "reason": self.reject})
        return web.json_response({"ok": True, "result": "applied", "reason": ""})

    # ------------------------------------------------------------ start en stop

    async def start(self, http_port=0):
        app = web.Application()
        app.router.add_route("*", "/huisdev/{tail:.*}", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", http_port)
        await site.start()
        self.http_port = site._server.sockets[0].getsockname()[1]
        if self.udp is None:
            esp = self

            class Proto(asyncio.DatagramProtocol):
                def connection_made(self, t):
                    self.t = t

                def datagram_received(self, data, addr):
                    esp.udp_seen.append(data)
                    if data.startswith(b"HUISDEV?"):
                        self.t.sendto(json.dumps(esp.discovery()).encode(), addr)

            self.udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(Proto, local_addr=("127.0.0.1", 0))
            self.udp_port = self.udp.get_extra_info("sockname")[1]
        return self

    async def stop_http(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def stop(self):
        await self.stop_http()
        if self.udp:
            self.udp.close()
            self.udp = None

    def announce(self, port: int):
        self.udp.sendto(json.dumps(self.discovery()).encode(), ("127.0.0.1", port))


def pool() -> FakeEsp:
    """Een poolcontroller zoals HUISDEV §10 hem beschrijft, met een paar punten en acties."""
    e = FakeEsp()
    e.point("ph", "pH", "float", 7.2, decimals=2, **{"class": "ph", "state": "measurement"}, stale=120000)
    e.point("temp", "Watertemperatuur", "float", 24.5, unit="°C", decimals=1, **{"class": "temperature", "state": "measurement"})
    e.point("orp", "Redox", "int", 740, unit="mV")
    e.point("pump", "Pomp", "bool", False, writable=True)
    e.point("mode", "Modus", "enum", 2, writable=True, options=[{"v": 0, "l": "Uit"}, {"v": 1, "l": "Aan"}, {"v": 2, "l": "Auto"}])
    e.point("ph_set", "pH-setpoint", "float", 7.2, decimals=1, min=6.8, max=7.6, step=0.1, writable=True)
    e.point("alarm_no_flow", "Alarm: geen doorstroming", "bool", False, group="alarmen")
    e.point("rssi", "WiFi-signaal", "int", -60, unit="dBm", diag=True)
    e.point("note", "Notitie", "text", "hallo")
    e.point("factory", "Fabrieksreset", "bool", False, writable=True, danger=True, confirm="Alles wissen?")
    e.action("boost", "Boost starten", args=[{"id": "minutes", "label": "Duur", "kind": "int", "unit": "min", "min": 5, "max": 240, "step": 5}])
    e.action("refresh", "Verversen")
    e.action("reset", "Fabrieksinstellingen", danger=True, confirm="Alle instellingen gaan verloren. Doorgaan?")
    return e
