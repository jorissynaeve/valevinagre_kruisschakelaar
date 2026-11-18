"""Een nagebootst HUISDEV-apparaat (protocol/huisdev/HUISDEV.md 1.0 en, na `scenes(...)`, de scènes van 1.1 §16): echte UDP en echte HTTP (aiohttp), gebouwd naar de spec en niet naar de add-on.

Wat de tests ermee kunnen: punten en acties toevoegen en weghalen (de `rev` volgt), waarden zetten, het apparaat uitzetten en weer aan, en
zien welke verzoeken er kwamen. De foutcodes zijn die van HUISDEV §4.

Scènes (`scenes(...)`, of de fabriek `kidswekker()`): slots, `store/play/stop/schedule` met `bad_slot`, `too_large`, `bad_data`, de FNV-hash van `scenes.hash_hex`,
`have/run/sched/time` in de state en een klok die de test zelf verzet (`now`; `None` is de echte tijd, `time_valid=False` een klok die nog niet gezet is). Een
geplande start waarvan de tijd gekomen is, start zodra de klok hem bereikt (`_advance`), zoals de header in `loop()` doet.

Protocol 1.2: `scenes(seek=True)` (of `kidswekker(seek=True)`) meldt `"seek": true` en kent `POST /huisdev/scene/seek` met `{"slot","t"}`, `{"data","t"}` of `{"t"}` (dezelfde scène als
de vorige seek): het beeld op t ms vasthouden (`run.seek`, `until` null), 422 `bad_value` voor een t buiten 0..86 400 000, `rejected` "geen scène om te tonen" bij `{"t"}` zonder eerdere
seek. Een `play` of `stop` wist het vasthouden, een geplande start vervangt het, en 300 s na de laatste seek eindigt het vanzelf (`seek_hold_s`, op de klok van het apparaat).
"""
import asyncio
import base64
import binascii
import hashlib
import json
import time

from aiohttp import web

from kkaddon import scenes as sc


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
        # scènes (HUISDEV §16)
        self.scene_cap: dict | None = None  # {"formats", "slots", "max_bytes", "sched"} of None: dit apparaat heeft geen scènes
        self.slots: dict[int, bytes] = {}
        self.run: dict | None = None  # {"slot", "h", "until"}
        self.sched: list[dict] = []
        self.now: float | None = None  # de klok van het apparaat (epoch); None = de echte tijd
        self.time_valid = True
        self.scene_calls: list[tuple[str, dict]] = []  # (store/play/stop/schedule, body) van elke scene-aanroep die tot het ontleden kwam
        self.writes: list[tuple[str, object]] = []  # ("store", slot) / ("schedule", lijst): wat er daadwerkelijk naar "flash" ging
        self.wrong_hash = False  # een apparaat dat een andere hash meldt dan wat we stuurden, en niets opslaat
        self.play_started: list[tuple[int, str]] = []  # (slot, hash) van elke start (ook een geplande)
        self.seeking: dict | None = None  # {"data": bytes, "slot": int, "t": ms, "at": epoch van de laatste seek}: het beeld dat nu wordt vastgehouden (run.seek)
        self.seek_hold_s = 300
        self.seek_log: list[dict] = []  # de body van elke seek die tot het ontleden kwam

    # ------------------------------------------------------------ wat het apparaat heeft

    def point(self, pid, label, kind, v=None, **kw):
        d = {"id": pid, "label": label, "kind": kind, **kw}
        self.points[pid] = {"def": d, "v": v, "age": 0}
        return self

    def action(self, aid, label, **kw):
        self.actions[aid] = {"def": {"id": aid, "label": label, **kw}}
        return self

    def scenes(self, formats=("ledscene/1",), slots=8, max_bytes=768, sched=8, seek=False):
        """Dit apparaat ondersteunt scènes (HUISDEV 1.1): het `scenes`-object in de descriptor; de `rev` rekent het mee. `seek`: protocol 1.2, `"seek": true` erbij."""
        self.scene_cap = {"formats": list(formats), "slots": slots, "max_bytes": max_bytes, "sched": sched}
        if seek:
            self.scene_cap["seek"] = True
        self.proto = "1.2" if seek else "1.1"
        return self

    def remove_point(self, pid):
        self.points.pop(pid, None)

    def set_value(self, pid, v, age=0):
        self.points[pid]["v"], self.points[pid]["age"] = v, age

    @property
    def rev(self) -> str:
        body = json.dumps([[p["def"] for p in self.points.values()], [a["def"] for a in self.actions.values()]] + ([self.scene_cap] if self.scene_cap else []), sort_keys=True)
        return hashlib.sha1(body.encode()).hexdigest()[:8]

    def descriptor(self) -> dict:
        d = {"proto": self.proto, "rev": self.rev, "device": {"id": self.hid, "type": self.type, "name": self.name, "fw": self.fw, "mac": "AA:BB:CC:A1:B2:C3", "lib": "1.0.0"},
             "points": [p["def"] for p in self.points.values()], "actions": [a["def"] for a in self.actions.values()]}
        if self.scene_cap:
            d["scenes"] = dict(self.scene_cap)
        return d

    def state(self) -> dict:
        pts = {}
        for pid, p in self.points.items():
            pts[pid] = {"v": p["v"], "age": p["age"]} if p["v"] is not None else {"v": None}
        out = {"proto": self.proto, "rev": self.rev, "up": self.up, "points": pts, "actions": {a: {"disabled": True, "reason": r} for a, r in self.disabled.items()}}
        if self.scene_cap:
            self._advance()
            sce = {"have": {str(n): {"h": sc.hash_hex(d), "n": len(d)} for n, d in sorted(self.slots.items())}, "time": int(self.epoch()) if self.time_valid else None}
            if self.run:
                sce["run"] = dict(self.run)
                if self.seeking:
                    sce["run"]["seek"] = self.seeking["t"]
            if self.sched:
                sce["sched"] = [dict(e) for e in self.sched]
            out["scenes"] = sce
        return out

    # ------------------------------------------------------------ scènes (§16)

    def epoch(self) -> float:
        return time.time() if self.now is None else self.now

    def _start(self, slot: int, data: bytes) -> str:
        """Een scène laten spelen (een slot, of rechtstreeks met slot 0). Dezelfde scène die al speelt: niets."""
        h = sc.hash_hex(data)
        if self.seeking:
            self.seeking = self.run = None  # een start (play, geplande start) vervangt het vasthouden, ook van dezelfde scène
        if self.run and self.run["h"] == h:
            return h
        s = sc.parse(data)
        total = sc.total_ms(s)
        self.run = {"slot": slot, "h": h, "until": int(self.epoch() + total / 1000) if total is not None and self.time_valid else None}
        self.play_started.append((slot, h))
        return h

    def _advance(self):
        """De klok van het apparaat: een scène die klaar is verdwijnt uit `run`, een geplande start waarvan de tijd gekomen is, start (en verdwijnt uit de lijst)."""
        if not self.time_valid:
            return
        now = self.epoch()
        if self.run and self.run["until"] is not None and now >= self.run["until"]:
            self.run = None
        if self.seeking and now >= self.seeking["at"] + self.seek_hold_s:
            self.seeking = self.run = None  # 300 s na de laatste seek: klaar (de fade-out sla ik over)
        for e in [e for e in self.sched if e["at"] <= now]:
            self.sched.remove(e)
            if e["slot"] in self.slots:
                self._start(e["slot"], self.slots[e["slot"]])

    def _scene_limit(self) -> int:
        return -(-self.scene_cap["max_bytes"] // 3) * 4 + 256

    @staticmethod
    def _slot_ok(cap, v) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= cap["slots"]

    @staticmethod
    def _decode(v):
        if not isinstance(v, str):
            return None
        try:
            return base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError):
            return None

    def _scene(self, op: str, body: dict):
        cap = self.scene_cap
        self.scene_calls.append((op, body))
        ok = lambda **kw: web.json_response({"ok": True, "result": "applied", "reason": "", **kw})  # noqa: E731
        no = lambda reason: web.json_response({"ok": False, "result": "rejected", "reason": reason})  # noqa: E731
        if self.reject:
            return no(self.reject)
        self._advance()
        if op == "store":
            if not self._slot_ok(cap, body.get("slot")):
                return self._err(422, "bad_slot", "slot buiten 1..%d" % cap["slots"])
            data = self._decode(body.get("data"))
            if data is None:
                return self._err(422, "bad_data", "geen base64")
            n = body["slot"]
            if data == b"":
                if n in self.slots:
                    del self.slots[n]
                    self.writes.append(("store", n))
                if self.run and self.run["slot"] == n:
                    self.run["slot"] = 0
                return ok()
            try:
                sc.parse(data, cap["max_bytes"])
            except sc.SceneError as e:
                return no(str(e))
            h = sc.hash_hex(data)
            if self.wrong_hash:
                return ok(hash="deadbeef")
            if self.slots.get(n) != data:  # dezelfde bytes: niets schrijven
                self.slots[n] = data
                self.writes.append(("store", n))
            if self.run and self.run["slot"] == n:
                self.run["slot"] = 0  # het apparaat speelt zijn eigen kopie
            return ok(hash=h)
        if op == "play":
            if "slot" in body:
                if not self._slot_ok(cap, body["slot"]):
                    return self._err(422, "bad_slot")
                if body["slot"] not in self.slots:
                    return no("slot is leeg")
                return ok(hash=self._start(body["slot"], self.slots[body["slot"]]))
            data = self._decode(body.get("data"))
            if data is None:
                return self._err(422, "bad_data", "geen base64")
            try:
                sc.parse(data, cap["max_bytes"])
            except sc.SceneError as e:
                return no(str(e))
            return ok(hash=self._start(0, data))
        if op == "stop":
            if "slot" in body:
                if not self._slot_ok(cap, body["slot"]):
                    return self._err(422, "bad_slot")
                if self.run and self.run["slot"] == body["slot"]:
                    self.run = self.seeking = None
            else:
                self.run = self.seeking = None
            return ok()
        if op == "seek":
            self.seek_log.append(dict(body))
            t = body.get("t")
            if not isinstance(t, int) or isinstance(t, bool) or not 0 <= t <= 86_400_000:
                return self._err(422, "bad_value", "t")
            if "slot" in body and "data" in body:
                return self._err(422, "bad_value", "slot en data")
            if "slot" in body:
                if not self._slot_ok(cap, body["slot"]):
                    return self._err(422, "bad_slot")
                if body["slot"] not in self.slots:
                    return no("slot is leeg")
                data, slot = self.slots[body["slot"]], body["slot"]
            elif "data" in body:
                data, slot = self._decode(body["data"]), 0
                if data is None:
                    return self._err(422, "bad_data", "geen base64")
                try:
                    sc.parse(data, cap["max_bytes"])
                except sc.SceneError as e:
                    return no(str(e))
            else:
                if not self.seeking:
                    return no("geen scène om te tonen")
                data, slot = self.seeking["data"], self.seeking["slot"]
            self.seeking = {"data": data, "slot": slot, "t": t, "at": self.epoch()}
            h = sc.hash_hex(data)
            self.run = {"slot": slot, "h": h, "until": None}
            return ok(hash=h)
        # schedule
        entries = body.get("entries")
        if not isinstance(entries, list):
            return self._err(422, "bad_value", "entries")
        if len(entries) > cap["sched"]:
            return self._err(422, "bad_value", "te veel regels")
        now = self.epoch()
        for e in entries:
            if not isinstance(e, dict) or not self._slot_ok(cap, e.get("slot")):
                return self._err(422, "bad_slot")
            at = e.get("at")
            if not isinstance(at, int) or isinstance(at, bool) or not 1_700_000_000 <= at <= now + 14 * 86400:
                return self._err(422, "bad_value", "at")
        new = sorted(({"slot": e["slot"], "at": e["at"]} for e in entries), key=lambda e: e["at"])
        if new != self.sched:  # dezelfde lijst: niets schrijven
            self.sched = new
            self.writes.append(("schedule", [dict(e) for e in new]))
        return ok()

    def discovery(self) -> dict:
        return {"huisdev": self.proto, "id": self.hid, "type": self.type, "name": self.name, "fw": self.fw, "rev": self.rev, "port": self.http_port, "claimed": bool(self.token)}

    # ------------------------------------------------------------ HTTP

    def _err(self, status, code, reason=""):
        return web.json_response({"ok": False, "error": code, "reason": reason}, status=status)

    async def _handle(self, r: web.Request) -> web.Response:
        body = {}
        raw = await r.read() if r.method == "POST" else b""
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
        if not isinstance(body, dict):
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
        if self.scene_cap and (path in ("/huisdev/scene/store", "/huisdev/scene/play", "/huisdev/scene/stop", "/huisdev/scene/schedule") or (path == "/huisdev/scene/seek" and self.scene_cap.get("seek"))):
            if not self.token:
                return self._err(403, "not_claimed", "eerst claimen")
            if not ok_tok:
                return self._err(401, "token")
            if len(raw) > self._scene_limit():
                return self._err(422, "too_large")  # voor het ontleden
            return self._scene(path.rsplit("/", 1)[1], body)
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


def kidswekker(slots=8, max_bytes=768, sched=8, seek=False) -> FakeEsp:
    """Een ledstrip-wekker met scènes (HUISDEV 1.1 §16): een paar punten en `ledscene/1` in acht slots; met `seek=True` protocol 1.2 (`scene/seek`)."""
    e = FakeEsp(hid="kidswekker-1a2b3c", type="kidswekker", name="Kidswekker", fw="1.0.0", proto="1.1")
    e.point("brightness", "Helderheid", "int", 80, unit="%", min=0, max=100, step=1, writable=True)
    e.point("armed", "Wekker aan", "bool", True)
    e.point("rssi", "WiFi-signaal", "int", -55, unit="dBm", diag=True)
    e.scenes(slots=slots, max_bytes=max_bytes, sched=sched, seek=seek)
    return e


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
