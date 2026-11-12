"""Een nagebootste ESPHome-webserver: echte HTTP (aiohttp) met `GET /events` als eventstroom en de REST-opdrachten van ESPHome, gebouwd naar wat een echt
toestel stuurt (opgenomen van een stopcontact met HLW8012, ESPHome 2026.x), niet naar de add-on.

Een nieuwe verbinding krijgt eerst een `ping` met de titel en dan per entiteit een volledige `state`-regel; daarna alleen wijzigingen (`push`) en
logregels, die de add-on moet negeren.
"""
import asyncio
import json

from aiohttp import web


class FakeEsphome:
    def __init__(self, title="powerplug04tv", auth=None):
        self.title = title
        self.auth = auth  # (gebruiker, wachtwoord) of None
        self.ents: dict[str, dict] = {}  # id ("sensor-power") -> volledige regel
        self.calls: list[tuple[str, str, dict]] = []  # (methode, pad, query)
        self.queues: list[asyncio.Queue] = []
        self.hits = 0  # aantal eventstromen dat is geopend
        self.runner = None
        self.port = 0
        self.fail_commands = 0  # zoveel opdrachten krijgen een 500

    # ------------------------------------------------------------ wat het toestel heeft

    def add(self, eid, name, **kw):
        self.ents[eid] = {"id": eid, "name": name, "icon": "", "entity_category": 0, **kw}
        return self

    def sensor(self, obj, name, value, uom="", decimals=1, **kw):
        state = f"{value:.{decimals}f}" + (f" {uom}" if uom else "")
        return self.add(f"sensor-{obj}", name, value=value, state=state, **({"uom": uom} if uom else {}), **kw)

    def switch(self, obj, name, on=False, **kw):
        return self.add(f"switch-{obj}", name, value=on, state="ON" if on else "OFF", assumed_state=False, **kw)

    def light(self, obj, name, on=False):
        return self.add(f"light-{obj}", name, state="ON" if on else "OFF", color_mode="onoff", color={}, effects=["None"])

    def select(self, obj, name, options, current, **kw):
        return self.add(f"select-{obj}", name, value=current, state=current, option=list(options), **kw)

    def number(self, obj, name, value, lo, hi, step, uom="", **kw):
        return self.add(f"number-{obj}", name, value=value, state=f"{value:g}" + (f" {uom}" if uom else ""), min_value=lo, max_value=hi, step=step, mode=0, **({"uom": uom} if uom else {}), **kw)

    def button(self, obj, name, **kw):
        return self.add(f"button-{obj}", name, **kw)

    def text_sensor(self, obj, name, value, **kw):
        return self.add(f"text_sensor-{obj}", name, value=value, state=value, **kw)

    def remove(self, eid):
        self.ents.pop(eid, None)

    def push(self, eid, **fields):
        """Een deelregel zoals ESPHome hem bij een wijziging stuurt: alleen id en wat veranderde."""
        e = self.ents[eid]
        e.update(fields)
        self._send({"id": eid, **fields})

    def _send(self, data, event="state"):
        for q in self.queues:
            q.put_nowait((event, data))

    # ------------------------------------------------------------ HTTP

    def _unauthorised(self, r: web.Request) -> bool:
        if not self.auth:
            return False
        import base64
        return r.headers.get("Authorization") != "Basic " + base64.b64encode(f"{self.auth[0]}:{self.auth[1]}".encode()).decode()

    async def _events(self, r: web.Request) -> web.StreamResponse:
        if self._unauthorised(r):
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="x"'})
        self.hits += 1
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})
        await resp.prepare(r)
        q: asyncio.Queue = asyncio.Queue()
        self.queues.append(q)
        try:
            await resp.write(f'retry: 30000\nid: 1\nevent: ping\ndata: {json.dumps({"title": self.title, "comment": "", "ota": True, "log": True, "lang": "en"})}\n\n'.encode())
            for e in list(self.ents.values()):
                await resp.write(f"event: state\ndata: {json.dumps(e)}\n\n".encode())
                await resp.write(b"id: 2\nevent: log\ndata: \x1b[0;36m[D][sensor:103]: 'Power': Sending state 0.68510 W\x1b[0m\n\n")
            while True:
                event, data = await q.get()
                if event == "close":
                    break
                await resp.write(f"event: {event}\ndata: {json.dumps(data) if isinstance(data, dict) else data}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.queues.remove(q)
        return resp

    async def _command(self, r: web.Request) -> web.Response:
        if self._unauthorised(r):
            return web.Response(status=401)
        domain, obj, verb = r.match_info["domain"], r.match_info["obj"], r.match_info["verb"]
        self.calls.append((r.method, r.path, dict(r.query)))
        eid = f"{domain}-{obj}"
        e = self.ents.get(eid)
        if r.method != "POST" or e is None:
            return web.Response(status=404)
        if self.fail_commands:
            self.fail_commands -= 1
            return web.Response(status=500)
        if verb in ("turn_on", "turn_off"):
            on = verb == "turn_on"
            self.push(eid, state="ON" if on else "OFF", **({"value": on} if "value" in e else {}))
        elif verb == "set" and domain == "select":
            if r.query.get("option") not in e["option"]:
                return web.Response(status=404)
            self.push(eid, value=r.query["option"], state=r.query["option"])
        elif verb == "set" and domain == "number":
            v = float(r.query["value"])
            self.push(eid, value=v, state=f"{v:g}")
        elif verb == "set" and domain == "text":
            self.push(eid, value=r.query["value"], state=r.query["value"])
        elif verb == "press" and domain == "button":
            pass
        else:
            return web.Response(status=404)
        return web.Response(status=200)

    # ------------------------------------------------------------ start en stop

    async def start(self, port=0):
        app = web.Application()
        app.router.add_get("/events", self._events)
        app.router.add_route("*", "/{domain}/{obj}/{verb}", self._command)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    def drop(self):
        """Alle eventstromen sluiten, zoals een toestel dat herstart."""
        self._send("", "close")

    async def stop(self):
        self.drop()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None


def plug() -> FakeEsphome:
    """Het stopcontact zoals het echt antwoordde: vermogensmeting, schakelaar, een rode led, een keuze, een restart-knop en wat diagnose."""
    e = FakeEsphome()
    e.light("red_led", "Red led")
    e.sensor("uptime", "Uptime", 181.298, "s", 0, entity_category=2)
    e.sensor("voltage", "Voltage", 232.1882, "V")
    e.sensor("apparent_power", "Apparent power", 10.75782, "VA")
    e.sensor("power_factor", "Power factor", 0.063684, "", 1)
    e.sensor("current", "Current", 0.046332, "A", 3)
    e.sensor("power", "Power", 0.685099, "W")
    e.sensor("energy", "Energy", 0.371095, "Wh")
    e.switch("powerplug04tv", "powerplug04tv", True)
    e.button("restart", "Restart", entity_category=1)
    e.text_sensor("reset_reason", "Reset Reason", "SW Reboot", entity_category=2)
    e.select("status_led_mode", "Status led mode", ["Normal", "Invert", "Off"], "Normal", entity_category=1)
    return e
