"""Een nagebootste Hue-bridge: echte HTTPS (aiohttp, zelfondertekend certificaat zoals Signify's bridge), `GET /api/config`, koppelen met de ronde knop,
`GET /clip/v2/resource`, de eventstroom `/eventstream/clip/v2` en `PUT` op een licht. De resources hebben de vorm van wat een echte bridge (BSB002, API 1.78)
antwoordde bij het uitlezen van een installatie: een apparaat met diensten (`light`, `zigbee_connectivity`, `motion`, ...), waarvan de gegevens in de dienst zitten.

Dingen die een echte bridge doet en de add-on moet verdragen: het antwoord komt in stukken (een lijst van 100 KB en meer), de eventstroom zwijgt als er niets
verandert (alleen `: hi` bij het verbinden) en een lamp die uit staat blijft zijn laatste stand melden, met `connectivity_issue`.
"""
import asyncio
import datetime
import json
import ssl
import time
import uuid
from hashlib import sha256
from pathlib import Path

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

NS = uuid.UUID("6f1f1e0e-0000-4000-8000-000000000001")


def make_cert(folder: Path, tag: str, cn: str):
    """Een zelfondertekend certificaat; geeft (certbestand, sleutelbestand, sha256 van het DER)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=3650)).sign(key, hashes.SHA256()))
    cf, kf = folder / f"{tag}.crt", folder / f"{tag}.key"
    cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kf.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cf, kf, sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def merge(old: dict, new: dict) -> dict:
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(old.get(k), dict):
            merge(old[k], v)
        else:
            old[k] = v
    return old


class FakeHue:
    def __init__(self, bridge_id="001788FFFE123456"):
        self.bridge_id = bridge_id
        self.key = "k" * 40
        self.link = False  # de ronde knop is ingedrukt
        self.res: dict[str, dict] = {}
        self.dev: dict[str, str] = {}  # naam -> id van het apparaat (bij dubbele namen de laatste)
        self.calls: list[tuple[str, str, object]] = []  # (methode, pad, body)
        self.times: list[float] = []
        self.queues: list[asyncio.Queue] = []
        self.streams = 0
        self.lists = 0  # aantal keer dat de hele lijst is opgehaald
        self.status_next = 0  # zoveel opdrachten krijgen deze HTTP-status
        self.status_code = 429
        self.ping_status = 0  # de bridge antwoordt op de vraag "ben je er nog" met deze status (de eventstroom blijft open)
        self.reject = 0  # zoveel opdrachten krijgen 200 met een foutmelding, zoals de bridge bij een lamp die niet bereikbaar is
        self.runner = None
        self.port = 0
        self.folder: Path | None = None
        self.fp = ""
        self._cert = None
        self._add_bridge()

    # ------------------------------------------------------------ wat de bridge heeft

    @staticmethod
    def _id(kind: str, name: str, n: int = 0) -> str:
        return str(uuid.uuid5(NS, f"{kind}:{name}:{n}"))

    def _device(self, name: str, model: str, archetype: str, services: list[tuple[str, dict]], n: int = 0) -> str:
        did = self._id("device", name, n)
        svc = []
        for rtype, body in services:
            rid = self._id(rtype, name, n)
            self.res[rid] = {"id": rid, "type": rtype, "owner": {"rid": did, "rtype": "device"}, **body}
            svc.append({"rid": rid, "rtype": rtype})
        self.res[did] = {"id": did, "type": "device", "metadata": {"name": name, "archetype": archetype},
                         "product_data": {"model_id": model, "manufacturer_name": "Signify Netherlands B.V.", "product_name": model, "product_archetype": archetype, "certified": True,
                                          "software_version": "1.122.2"}, "services": svc}
        self.dev[name] = did
        return did

    def _add_bridge(self):
        self._device("Hue Bridge", "BSB002", "bridge_v2", [("bridge", {"bridge_id": self.bridge_id.lower(), "time_zone": {"time_zone": "Europe/Brussels"}})])

    @staticmethod
    def _zigbee(connected: bool) -> tuple[str, dict]:
        return "zigbee_connectivity", {"status": "connected" if connected else "connectivity_issue", "mac_address": "00:17:88:01:00:00:00:01"}

    def add_lamp(self, name, on=False, bri=50.0, connected=True, dimming=True, archetype="classic_bulb", model="LWA024", n=0) -> str:
        light = {"metadata": {"name": name, "archetype": archetype}, "on": {"on": on}, "mode": "normal"}
        if dimming:
            light["dimming"] = {"brightness": bri, "min_dim_level": 5.0}
        return self._device(name, model, archetype, [("light", light), self._zigbee(connected)], n)

    def add_plug(self, name, on=False, connected=True, archetype="plug", n=0) -> str:
        """Een stopcontact is voor de bridge een licht zonder `dimming`; zijn archetype kiest de gebruiker (`plug`, `string_light`, `classic_bulb`)."""
        return self.add_lamp(name, on=on, connected=connected, dimming=False, archetype=archetype, model="OSP 240", n=n)

    def add_sensor(self, name, motion=False, temp=9.27, light_level=0, battery=100, connected=True, n=0) -> str:
        return self._device(name, "SML003", "unknown_archetype", [
            ("motion", {"enabled": True, "motion": {"motion": motion, "motion_valid": True, "motion_report": {"changed": "2026-11-11T21:57:05.216Z", "motion": motion}}}),
            ("temperature", {"enabled": True, "temperature": {"temperature": temp, "temperature_valid": True, "temperature_report": {"changed": "2026-11-12T14:01:31.006Z", "temperature": temp}}}),
            ("light_level", {"enabled": True, "light": {"light_level": light_level, "light_level_valid": True}}),
            ("device_power", {"power_state": {"battery_state": "normal", "battery_level": battery}}),
            self._zigbee(connected)], n)

    def add_switch(self, name) -> str:
        return self._device(name, "RWL021", "unknown_archetype", [("button", {"metadata": {"control_id": 1}, "button": {"last_event": "short_release"}}),
                                                                    ("device_power", {"power_state": {"battery_state": "normal", "battery_level": 80}}), self._zigbee(True)])

    def add_room(self, name: str, lamps: list[str], on=False, bri=50.0) -> str:
        """Een kamer: de lampen als `children` en één `grouped_light` (de stand van de hele kamer) als dienst."""
        rid, gl = self._id("room", name), self._id("grouped_light", name)
        self.res[gl] = {"id": gl, "type": "grouped_light", "owner": {"rid": rid, "rtype": "room"}, "on": {"on": on}, "dimming": {"brightness": bri}, "alert": {"action_values": ["breathe"]}}
        self.res[rid] = {"id": rid, "type": "room", "metadata": {"name": name, "archetype": "living_room"}, "children": [{"rid": self.dev[x], "rtype": "device"} for x in lamps],
                         "services": [{"rid": gl, "rtype": "grouped_light"}]}
        self.dev[name] = rid
        return rid

    def add_scene(self, room: str, name: str, active: str = "inactive") -> str:
        sid = self._id("scene", f"{room}/{name}")
        self.res[sid] = {"id": sid, "type": "scene", "metadata": {"name": name}, "group": {"rid": self.dev[room], "rtype": "room"}, "status": {"active": active}, "actions": []}
        return sid

    def group(self, room: str) -> dict:
        return self.svc(self.dev[room], "grouped_light")

    def svc(self, dev_id: str, rtype: str) -> dict:
        return next(self.res[s["rid"]] for s in self.res[dev_id]["services"] if s["rtype"] == rtype)

    # ------------------------------------------------------------ wijzigingen met een gebeurtenis

    def push(self, kind: str, objs: list[dict]):
        env = [{"creationtime": "2026-11-12T14:00:00Z", "data": objs, "id": str(uuid.uuid4()), "type": kind}]
        for q in self.queues:
            q.put_nowait(f"data: {json.dumps(env)}\n\n")

    def update(self, dev_id: str, rtype: str, **fields):
        """Een dienst van een apparaat wijzigt: de nieuwe stand erin en een `update` over de eventstroom met alleen wat veranderde."""
        r = self.svc(dev_id, rtype)
        merge(r, json.loads(json.dumps(fields)))
        self.push("update", [{"id": r["id"], "type": rtype, "owner": r["owner"], **fields}])

    def connected(self, dev_id: str, ok: bool):
        self.update(dev_id, "zigbee_connectivity", status="connected" if ok else "connectivity_issue")

    def remove(self, dev_id: str):
        d = self.res.pop(dev_id)
        gone = [{"id": dev_id, "type": "device"}]
        for s in d["services"]:
            self.res.pop(s["rid"], None)
            gone.append({"id": s["rid"], "type": s["rtype"]})
        self.push("delete", gone)

    # ------------------------------------------------------------ HTTPS

    def _unauthorised(self, r: web.Request) -> bool:
        return r.headers.get("hue-application-key") != self.key

    async def _config(self, r: web.Request) -> web.Response:
        return web.json_response({"name": "Hue Bridge", "datastoreversion": "198", "swversion": "1978545030", "apiversion": "1.78.0", "mac": "00:17:88:74:40:71", "bridgeid": self.bridge_id,
                                  "factorynew": False, "replacesbridgeid": None, "modelid": "BSB002", "starterkitid": ""})

    async def _pair(self, r: web.Request) -> web.Response:
        body = await r.json()
        self.calls.append(("POST", "/api", body))
        if not self.link:
            return web.json_response([{"error": {"type": 101, "address": "", "description": "link button not pressed"}}])
        return web.json_response([{"success": {"username": self.key, "clientkey": "0D4850DCD927E4AB9B2FE358EDA7786F"}}])

    async def _whitelist(self, r: web.Request) -> web.Response:
        self.calls.append((r.method, r.path, None))
        return web.json_response([{"success": f"/config/whitelist/{r.match_info['k2']} deleted"}])

    async def _list(self, r: web.Request) -> web.StreamResponse:
        if self._unauthorised(r):
            return web.json_response({"errors": [{"description": "unauthorized user"}]}, status=403)
        self.lists += 1
        body = json.dumps({"errors": [], "data": list(self.res.values())}).encode()
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(r)
        for i in range(0, len(body), 4096):  # in stukken, zoals een bridge: een enkele read() ziet maar een deel
            await resp.write(body[i:i + 4096])
            await asyncio.sleep(0)
        return resp

    async def _bridge(self, r: web.Request) -> web.Response:
        if self._unauthorised(r):
            return web.json_response({"errors": [{"description": "unauthorized user"}]}, status=403)
        self.calls.append(("GET", r.path, None))
        if self.ping_status:
            return web.json_response({}, status=self.ping_status)
        return web.json_response({"errors": [], "data": [x for x in self.res.values() if x["type"] == "bridge"]})

    async def _put_light(self, r: web.Request) -> web.Response:
        if self._unauthorised(r):
            return web.json_response({"errors": [{"description": "unauthorized user"}]}, status=403)
        body = await r.json()
        self.calls.append(("PUT", r.path, body))
        self.times.append(time.monotonic())
        light = self.res.get(r.match_info["id"])
        if light is None or light["type"] != "light":
            return web.json_response({"errors": [{"description": "resource not found"}]}, status=404)
        if self.status_next:
            self.status_next -= 1
            return web.json_response({"errors": [{"description": "busy"}]} if self.status_code == 429 else {}, status=self.status_code)
        if self.reject:
            self.reject -= 1
            return web.json_response({"errors": [{"description": "device (light) is unreachable"}], "data": []})
        merge(light, json.loads(json.dumps(body)))
        self.push("update", [{"id": light["id"], "type": "light", "owner": light["owner"], **body}])
        return web.json_response({"errors": [], "data": [{"rid": light["id"], "rtype": "light"}]})

    def _answer(self, r: web.Request, body) -> web.Response | None:
        """Wat elke opdracht eerst doet: de sleutel, het logboek en de storingen die een test kan inschakelen."""
        self.calls.append(("PUT", r.path, body))
        self.times.append(time.monotonic())
        if self.status_next:
            self.status_next -= 1
            return web.json_response({"errors": [{"description": "busy"}]} if self.status_code == 429 else {}, status=self.status_code)
        if self.reject:
            self.reject -= 1
            return web.json_response({"errors": [{"description": "device (light) is unreachable"}], "data": []})
        return None

    async def _put_group(self, r: web.Request) -> web.Response:
        if self._unauthorised(r):
            return web.json_response({"errors": [{"description": "unauthorized user"}]}, status=403)
        body = await r.json()
        gl = self.res.get(r.match_info["id"])
        if gl is None or gl["type"] != "grouped_light":
            return web.json_response({"errors": [{"description": "resource not found"}]}, status=404)
        bad = self._answer(r, body)
        if bad is not None:
            return bad
        merge(gl, json.loads(json.dumps(body)))
        self.push("update", [{"id": gl["id"], "type": "grouped_light", "owner": gl["owner"], **body}])
        return web.json_response({"errors": [], "data": [{"rid": gl["id"], "rtype": "grouped_light"}]})

    async def _put_scene(self, r: web.Request) -> web.Response:
        """Een scène activeren: de kamer gaat aan, deze scène is actief en de andere van die kamer niet meer."""
        if self._unauthorised(r):
            return web.json_response({"errors": [{"description": "unauthorized user"}]}, status=403)
        body = await r.json()
        sc = self.res.get(r.match_info["id"])
        if sc is None or sc["type"] != "scene":
            return web.json_response({"errors": [{"description": "resource not found"}]}, status=404)
        bad = self._answer(r, body)
        if bad is not None:
            return bad
        if (body.get("recall") or {}).get("action") == "active":
            room = sc["group"]["rid"]
            ups = []
            for o in self.res.values():
                if o["type"] == "scene" and o["group"]["rid"] == room:
                    o["status"]["active"] = "static" if o is sc else "inactive"
                    ups.append({"id": o["id"], "type": "scene", "status": {"active": o["status"]["active"]}})
            gl = next(x for x in self.res.values() if x["type"] == "grouped_light" and x["owner"]["rid"] == room)
            gl["on"]["on"] = True
            ups.append({"id": gl["id"], "type": "grouped_light", "owner": gl["owner"], "on": {"on": True}})
            self.push("update", ups)
        return web.json_response({"errors": [], "data": [{"rid": sc["id"], "rtype": "scene"}]})

    async def _events(self, r: web.Request) -> web.StreamResponse:
        if self._unauthorised(r):
            return web.json_response({"errors": [{"description": "unauthorized user"}]}, status=403)
        self.streams += 1
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store"})
        await resp.prepare(r)
        q: asyncio.Queue = asyncio.Queue()
        self.queues.append(q)
        try:
            await resp.write(b": hi\n\n")  # daarna zwijgt de bridge tot er iets verandert
            while True:
                chunk = await q.get()
                if chunk == "close":
                    break
                await resp.write(chunk.encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.queues.remove(q)
        return resp

    # ------------------------------------------------------------ start en stop

    def new_cert(self):
        """Een ander certificaat voor de volgende start, zoals na een reset van de bridge."""
        self._cert = make_cert(self.folder, f"c{id(self)}_{int(time.monotonic() * 1000)}", self.bridge_id)
        self.fp = self._cert[2]

    async def start(self, folder: Path | None = None, port=0):
        if folder is not None:
            self.folder = Path(folder)
        if self._cert is None:
            self.new_cert()
        app = web.Application()
        app.router.add_get("/api/config", self._config)
        app.router.add_post("/api", self._pair)
        app.router.add_delete("/api/{k1}/config/whitelist/{k2}", self._whitelist)
        app.router.add_get("/clip/v2/resource", self._list)
        app.router.add_get("/clip/v2/resource/bridge", self._bridge)
        app.router.add_put("/clip/v2/resource/light/{id}", self._put_light)
        app.router.add_put("/clip/v2/resource/grouped_light/{id}", self._put_group)
        app.router.add_put("/clip/v2/resource/scene/{id}", self._put_scene)
        app.router.add_get("/eventstream/clip/v2", self._events)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self._cert[0], self._cert[1])
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port, ssl_context=ctx)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    def drop(self):
        """Alle eventstromen sluiten, zoals een bridge die herstart."""
        for q in self.queues:
            q.put_nowait("close")

    async def stop(self):
        self.drop()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None


def house() -> FakeHue:
    """Een klein huis, met van elke soort wat een echte installatie ook had: lampen (twee met dezelfde naam), een stopcontact dat zich als lamp voordoet, een lamp
    die uit staat, een bewegingsmelder en een dimmer (die we niet aanbieden)."""
    h = FakeHue()
    h.add_lamp("Keuken", on=False, bri=40.0)
    h.add_lamp("Gang", on=True, bri=100.0, connected=False)
    h.add_lamp("Oprit", on=False, bri=9.88, n=1)
    h.add_lamp("Oprit", on=False, bri=9.88, n=2)
    h.add_plug("Stopcontact tuin", on=True, archetype="classic_bulb")
    h.add_sensor("Melder voordeur", motion=False, temp=9.27, light_level=0, battery=100)
    h.add_switch("Dimmer hal")
    return h
