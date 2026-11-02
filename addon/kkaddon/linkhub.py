"""Koppelingen met andere Huisconfig-installaties (een ander subnet, met een eigen HA en add-on).

Elke installatie
  * luistert op een eigen TCP-poort (`link_port`, standaard 47490) en verbindt met de adressen die je opgeeft. Eén kant opgeven
    volstaat: de verbinding werkt in beide richtingen. Geven beide kanten elkaar op, dan blijft de verbinding over die is
    opgezet door de installatie met de kleinste id (aan beide kanten dezelfde regel), de andere wordt gesloten.
  * biedt haar lampen (kringen en lichten op het netwerk) en Tuya-toestellen aan in een catalogus (naam en soort). De andere kant
    kiest wat ze wil gebruiken (`want`); alleen daarvan gaat de stand over de lijn, elke keer dat er iets verandert.
  * voert bevelen voor haar **eigen** toestellen uit, en alleen voor wat de andere kant gekozen heeft: de eigenaar beslist, met
    dezelfde regels als in de eigen UI (een lamp met een aan/uit-ingang laat zich niet schakelen, een Tuya-datapunt alleen binnen
    zijn datamodel).

Een **gang** (`e:<HA-entiteit>`: de touch en het relais van een schakelaar, een Tuya-relais, een relais van een NSPanel) kan lid
worden van een kring bij een andere installatie: een touch hier bedient een lamp daar, of omgekeerd. Een wijziging van zo'n gang
gaat meteen weg (niet pas bij de volgende tik), want het is een druk. Een gang die hier in een actieve kring zit is bezet: een
andere installatie kan ze kiezen om te tonen, maar de eigenaar weigert er bevelen voor (één eigenaar per entiteit, zoals in
model._conflicts).

Wat je van een andere installatie kiest, verschijnt hier als alleen-lezen blok (`r:<installatie>:<object>`), te bedienen en aan een
kaart te hangen. Instellen gebeurt bij de eigenaar. Valt de verbinding weg, dan blijft het blok staan met de laatst bekende
naam, als offline; een bevel gaat dan niet weg en wordt ook niet bewaard.

De koppelsleutel, de naam van deze installatie en de adressen staan in /data/link.json (0600), niet in het model: de sleutel
hoort niet in de export.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .engine import Engine
from .link import ID_RE, MAX_MESSAGE, OBJ_RE, LinkError, Session, derive, open_hello, seal_hello, split_ref
from .model import ModelError, panel_relay_entity
from .schema import _tuya_relays, tuya_entity

log = logging.getLogger("kk.link")

LINK_PORT = 47490
HEARTBEAT = 15.0
HANDSHAKE = 10.0
CMD_TIMEOUT = 10.0
RETRY = (2.0, 60.0)
MAX_PEERS = 16
MAX_OBJECTS = 2000
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")[:40]


@dataclass
class Conn:
    ws: Any
    sess: Session
    peer: str
    initiator: str  # id van wie de verbinding opzette: beslist welke blijft als er twee zijn
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sent: str = ""  # laatst gestuurde stand: alleen een wijziging gaat opnieuw weg
    sent_catalog: str = ""
    want: set[str] = field(default_factory=set)  # wat de andere kant gekozen heeft: alleen dat krijgt ze en mag ze bedienen
    futures: dict[int, asyncio.Future] = field(default_factory=dict)


@dataclass
class Address:
    host: str
    port: int
    state: str = "nog niet geprobeerd"
    error: str = ""
    peer: str | None = None
    task: asyncio.Task | None = None


class LinkHub:
    def __init__(self, data_dir: Path, engine: Engine, port: int = LINK_PORT, retry: tuple[float, float] = RETRY, clock=time.monotonic) -> None:
        self.path = Path(data_dir) / "link.json"
        self.engine, self.model = engine, engine.model
        self.port, self.retry, self._clock = port, retry, clock
        self.tuya = None  # TuyaHub: zijn toestellen delen en bedienen
        self.ha = None  # HaBridge: de stand van de gangen, en ze schakelen (attach_ha)
        self.me = {"id": "", "name": ""}
        self.key_text = ""
        self.addresses: list[Address] = []
        self.known: dict[str, dict] = {}  # installatie -> {name, objects: {obj: {kind, name}}}: haar catalogus, blijft na een herstart
        self.use: dict[str, list[str]] = {}  # installatie -> wat deze installatie van haar gebruikt
        self.remote: dict[str, dict] = {}  # installatie -> {obj: stand}, alleen zolang verbonden
        self.conns: dict[str, Conn] = {}
        self._fed: dict[str, str] = {}  # r:<installatie>:e:<entiteit> -> laatst aan de kringen gegeven stand
        self.listening, self.error = False, ""
        self._runner: web.AppRunner | None = None
        self._tasks: set[asyncio.Task] = set()
        self._seq = 0
        self._load()
        engine.link_view = self.status
        engine.remote_call = self._remote_call
        engine.subscribe(self._on_engine)

    # ------------------------------------------------------------ bestand

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.error("%s onleesbaar: %s", self.path, e)
            return
        self.me = {"id": str(raw.get("id") or ""), "name": str(raw.get("name") or "")}
        self.key_text = str(raw.get("key") or "")
        self.addresses = [Address(str(p["host"]), int(p["port"])) for p in raw.get("peers") or [] if isinstance(p, dict) and p.get("host")]
        self.known = {k: v for k, v in (raw.get("known") or {}).items() if ID_RE.match(k) and isinstance(v, dict)}
        self.use = {k: [o for o in v if isinstance(o, str) and OBJ_RE.match(o)] for k, v in (raw.get("use") or {}).items() if ID_RE.match(k) and isinstance(v, list)}
        if "use" not in raw:  # 0.12.0 gebruikte alles: wat toen al op het schema of op een kaart stond, blijft gekozen
            for ref in self._refs_in_model():
                peer, obj = split_ref(ref) or (None, None)
                if peer and obj not in self.use.setdefault(peer, []):
                    self.use[peer].append(obj)
            if self.use:
                log.info("koppelingen: in gebruik gehouden wat al op het schema of een kaart stond: %s", self.use)
                self._save()

    def _refs_in_model(self) -> set[str]:
        m = self.model
        refs = {k for k in m.schema["blocks"] if k.startswith("r:")}
        for c in m.cards.values():
            refs |= {it["ref"] for it in c.get("items", []) if c["type"] == "grid" and (it.get("ref") or "").startswith("r:")}
            if c["type"] == "charger":  # de toestellen op de ingangen en op start/stop
                devs = [r.partition("@")[2] for r in c["inputs"].values()] + [c["target"]["device"]]
                refs |= {d for d in devs if d.startswith("r:")}
            for it in c.get("items", []):  # een waarde op een knop of een energierij
                for d in (it.get("ref") if it.get("dp") else None, it.get("tuya")):
                    if d and d.startswith("r:"):
                        refs.add(d)
        for mm in m.modules.values():  # een Tuya-waarde, of een ingang die rechtstreeks een datapunt leest
            devs = [mm.get("device") or ""] + [r.partition("@")[2] for r in mm["inputs"].values() if r.startswith("v:")]
            refs |= {d for d in devs if d.startswith("r:")}
        for a in m.actions.values():
            if a["device"].startswith("r:"):
                refs.add(a["device"])
        return refs

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        d = {**self.me, "key": self.key_text, "peers": [{"host": a.host, "port": a.port} for a in self.addresses], "known": self.known, "use": self.use}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".link-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False, sort_keys=True)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _key(self) -> bytes | None:
        if not self.key_text:
            return None
        try:
            return derive(self.key_text)
        except LinkError:
            return None

    def ready(self) -> bool:
        return bool(self._key() and self.me["id"])

    # ------------------------------------------------------------ instellen (vanuit de UI)

    def configure(self, b: dict) -> None:
        """`name`: de naam van deze installatie (de id volgt er eenmalig uit en blijft dan). `key`: de koppelsleutel; leeg = behouden, `null` = wissen."""
        restart = False
        if "name" in b:
            name = b["name"]
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
                raise ModelError("geef deze installatie een naam (hoogstens 60 tekens)")
            self.me["name"] = name.strip()
            if not self.me["id"]:
                self.me["id"] = _slug(name) or "huis"
                restart = True
        if "key" in b:
            if b["key"] is None:
                self.key_text, restart = "", True
            elif isinstance(b["key"], str) and b["key"].strip():
                try:
                    derive(b["key"])
                except LinkError as e:
                    raise ModelError(str(e)) from e
                self.key_text, restart = b["key"].strip(), True
        self._save()
        if restart:
            self._restart()
        self._poke()

    def add_peer(self, host, port=LINK_PORT) -> None:
        host = str(host or "").strip()
        if not HOST_RE.match(host):
            raise ModelError("geef het IP-adres of de hostnaam van de andere Home Assistant")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ModelError("poort tussen 1 en 65535")
        if any(a.host == host and a.port == port for a in self.addresses):
            raise ModelError("dat adres staat er al")
        if len(self.addresses) >= MAX_PEERS:
            raise ModelError(f"maximaal {MAX_PEERS} adressen")
        a = Address(host, port)
        self.addresses.append(a)
        self._save()
        self._start_client(a)
        self._poke()

    def remove_peer(self, host, port) -> bool:
        a = next((x for x in self.addresses if x.host == host and x.port == port), None)
        if a is None:
            return False
        self.addresses.remove(a)
        if a.task:
            a.task.cancel()
        self._save()
        self._poke()
        return True

    def forget(self, pid: str) -> bool:
        """Een installatie die niet meer bestaat: haar blokken verdwijnen (de draden naar haar toestellen blijven, als 'weg')."""
        if pid not in self.known or pid in self.conns:
            return False
        del self.known[pid]
        self.remote.pop(pid, None)
        self.use.pop(pid, None)
        self._save()
        self._poke()
        return True

    def set_use(self, pid: str, obj: str, on: bool) -> None:
        """Een toestel van een andere installatie wel of niet gebruiken. Alleen wat je gebruikt wordt een blok en komt over de lijn."""
        if pid not in self.known or not isinstance(obj, str) or obj not in (self.known[pid].get("objects") or {}):
            raise ModelError("dat toestel biedt die installatie niet aan")
        cur = set(self.use.get(pid, []))
        cur = cur | {obj} if on else cur - {obj}
        self.use[pid] = sorted(cur)
        if not on:
            self.remote.get(pid, {}).pop(obj, None)
            if obj.startswith("e:"):
                self.engine.ha_state(f"r:{pid}:{obj}", "unavailable")
        self._save()
        conn = self.conns.get(pid)
        if conn is not None:
            self._spawn(self._send_want(conn))
        self._poke()

    def used(self, pid: str) -> set[str]:
        return set(self.use.get(pid, []))

    async def _send_want(self, conn: Conn) -> None:
        try:
            await self._send(conn, {"t": "want", "objects": sorted(self.used(conn.peer))})
        except (ConnectionError, RuntimeError) as e:
            log.debug("keuze naar %s niet verstuurd: %s", conn.peer, e)

    # ------------------------------------------------------------ voor de pagina en de kaarten

    def status(self) -> dict:
        remote = {}
        for pid in sorted(set(self.known) | set(self.remote)):
            up, use = pid in self.conns, self.used(pid)
            catalog = self.known.get(pid, {}).get("objects") or {}
            objs = {o: {**k, "online": False} for o, k in catalog.items() if o in use}
            if up:
                objs.update({o: v for o, v in self.remote.get(pid, {}).items() if o in use})
            remote[pid] = {"name": self.known.get(pid, {}).get("name") or pid, "online": up, "objects": objs,
                           "catalog": {o: {**k, "used": o in use} for o, k in sorted(catalog.items())}}
        return {"me": {**self.me, "port": self.port, "has_key": bool(self.key_text), "listening": self.listening, "error": self.error},
                "peers": [{"host": a.host, "port": a.port, "state": a.state, "error": a.error, "peer": a.peer} for a in self.addresses],
                "remote": remote}

    def is_up(self, pid: str) -> bool:
        return pid in self.conns

    def object(self, pid: str, obj: str) -> dict | None:
        """De stand van een object van een andere installatie; offline (met de laatst bekende naam) als de verbinding weg is.
        None als deze installatie het niet gebruikt."""
        if obj not in self.used(pid):
            return None
        if pid in self.conns and obj in self.remote.get(pid, {}):
            return self.remote[pid][obj]
        k = (self.known.get(pid, {}).get("objects") or {}).get(obj)
        return {**k, "online": False} if k else None

    def peer_name(self, pid: str) -> str:
        return self.known.get(pid, {}).get("name") or pid

    def send(self, pid: str, obj: str, **cmd) -> bool:
        """Een bevel zonder op het antwoord te wachten (een druk op een paneel). False als er geen verbinding is."""
        if pid not in self.conns or obj not in self.used(pid):
            return False
        self._spawn(self._command_logged(pid, obj, cmd))
        return True

    async def _command_logged(self, pid: str, obj: str, cmd: dict) -> None:
        try:
            await self.command(pid, obj, **cmd)
        except LinkError as e:
            log.warning("bevel naar %s %s: %s", self.peer_name(pid), obj, e)

    async def command(self, pid: str, obj: str, **cmd) -> None:
        """Een bevel aan de eigenaar; LinkError als het niet aankomt of geweigerd wordt."""
        conn = self.conns.get(pid)
        if conn is None:
            raise LinkError(f"geen verbinding met {self.peer_name(pid)}")
        if obj not in self.used(pid):
            raise LinkError("dit toestel is hier niet gekozen")
        self._seq += 1
        i = self._seq
        fut = asyncio.get_running_loop().create_future()
        conn.futures[i] = fut
        try:
            await self._send(conn, {"t": "cmd", "i": i, "obj": obj, **cmd})
            ack = await asyncio.wait_for(fut, CMD_TIMEOUT)
        except asyncio.TimeoutError as e:
            raise LinkError(f"{self.peer_name(pid)} antwoordt niet") from e
        finally:
            conn.futures.pop(i, None)
        if not ack.get("ok"):
            raise LinkError(str(ack.get("error") or "geweigerd"))

    # ------------------------------------------------------------ wat deze installatie deelt

    def attach_ha(self, ha) -> None:
        """De gangen van deze installatie staan in haar HA: hun stand lezen, ze schakelen, en een wijziging meteen doorgeven."""
        self.ha = ha
        ha.state_listeners.append(self._on_ha_state)

    def _on_ha_state(self, entity: str | None) -> None:
        obj = f"e:{entity}" if entity else None
        for c in list(self.conns.values()):
            if obj is None or obj in c.want:
                self._spawn(self._push_safe(c))  # een druk op een touch: niet wachten op de volgende tik

    def gangs(self) -> dict[str, str]:
        """Entiteit -> naam van elke gang die deze installatie aanbiedt: schakelaars uit HA, externe apparaten, Tuya-relais, NSPanel-relais."""
        m, out = self.model, {}
        for vid, gs in sorted(m.virtual.items()):
            for i, g in enumerate(gs):
                out.setdefault(g["entity"], f"{m.node_name(vid)} · {g['name'] or f'Gang {i + 1}'}")
        for sid, s in sorted(m.schema["switches"].items()):
            dev = m.schema["names"].get(f"d:{sid}") or s["name"]
            for g in s["gangs"]:
                out.setdefault(g["entity"], f"{dev} · {g['name'] or g['entity']}")
        for t in (self.tuya.status() if self.tuya else []):
            for p in _tuya_relays(t):
                out.setdefault(tuya_entity(t["id"], p["dp"]), f"{t['name']} · {p['name']}")
        for pid, p in sorted(m.panels.items()):
            for n in (1, 2):
                out.setdefault(panel_relay_entity(pid, n), f"{p['name']} · Relais {n}")
        return out

    def _busy(self) -> set[str]:
        """Entiteiten in een actieve kring van deze installatie: die stuurt hier al iemand."""
        return {x for g in self.model.groups.values() if g["enabled"] for x in g["entities"]}

    def export(self) -> dict[str, dict]:
        e, out = self.engine, {}
        for g in e.groups.view():
            basis = [m for m in g["members"] if m["lamp"] and m["on"] is not None] or g["members"]
            on = None if all(m["on"] is None for m in basis) else any(m["on"] for m in basis)
            out[f"g:{g['id']}"] = {"kind": "lamp", "name": g["name"], "on": on, "online": any(m["avail"] for m in g["members"]), "held": g.get("held") is not None}
        for (n, r), l in sorted(self.model.lights.items()):
            info = e.nodes.get(n)
            out[f"l{n}.{r}"] = {"kind": "lamp", "name": l.name or f"{self.model.node_name(n)} relais {r + 1}", "on": e.light_state(n, r), "online": bool(info and info.online), "held": False}
        for t in (self.tuya.status() if self.tuya else []):
            pts = [{k: p[k] for k in ("dp", "code", "name", "value", "unit", "rw", "type", "options", "min", "max", "step", "hide", "diag")} for p in t["points"]]
            out[f"y:{t['id']}"] = {"kind": "tuya", "name": t["name"], "online": t["online"], "product": t["product"], "points": pts}
        states, busy = (self.ha.states if self.ha else {}), self._busy()
        for ent, name in self.gangs().items():
            st = states.get(ent)
            up = st is not None and st[0] not in ("unavailable", "unknown")
            out[f"e:{ent}"] = {"kind": "gang", "name": name[:60], "on": (st[0] == "on") if up else None, "online": up, "busy": ent in busy}
        return out

    def _execute(self, conn: Conn, m: dict) -> str | None:
        """Een bevel van een andere installatie voor een eigen toestel. Geeft de reden van een weigering, of None."""
        obj, op, e, peer = m.get("obj"), m.get("op"), self.engine, conn.peer
        if not isinstance(obj, str) or not OBJ_RE.match(obj):
            return "ongeldig bevel"
        if obj not in conn.want:
            return "dit toestel is niet gekozen bij de andere installatie"
        log.info("bevel van %s: %s %s", self.peer_name(peer), obj, op or f"{m.get('dp')}={m.get('value')}")
        if obj.startswith("y:"):
            if self.tuya is None or obj[2:] not in self.tuya.devices:
                return "onbekend Tuya-toestel"
            try:
                self.tuya.set(obj[2:], str(m.get("dp")), str(m.get("value")))
            except ModelError as err:
                return str(err)
            return None
        if op not in ("on", "off", "toggle"):
            return "ongeldig bevel"
        on = None if op == "toggle" else op == "on"
        if obj.startswith("e:"):
            ent = obj[2:]
            if ent not in self.gangs():
                return "onbekende gang"
            if ent in self._busy():
                return "deze gang hoort bij een actieve kring van die installatie"
            st = self.ha.states.get(ent) if self.ha else None
            if on is None:
                if st is None or st[0] not in ("on", "off"):
                    return "stand van de gang onbekend"
                on = st[0] != "on"
            return None if (self.ha and self.ha.call(ent, on)) else "geen verbinding met HA"
        if obj.startswith("g:"):
            gid = obj[2:]
            if gid not in self.model.groups:
                return "onbekende lamp"
            if e.groups.held(gid) is not None:
                return "deze lamp wordt vastgehouden door zijn aan/uit-ingang"
            return None if e.groups.command(gid, on) else "geen enkel relais bereikbaar"
        n, r = (int(x) for x in obj[1:].split("."))
        if (n, r) not in self.model.lights:
            return "onbekende lamp"
        ok = e.toggle_light(n, r) if on is None else e.command_light(n, r, on)
        return None if ok else "lamp niet bereikbaar"

    # ------------------------------------------------------------ netwerk

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/link", self._accept)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, "0.0.0.0", self.port).start()
            self.listening, self.error = True, ""
            log.info("koppelingen: TCP :%d", self.port)
        except OSError as e:
            self.listening, self.error = False, f"poort {self.port} niet te openen: {e}"
            log.error("koppelingen: %s", self.error)
        for a in self.addresses:
            self._start_client(a)

    async def close(self) -> None:
        for a in self.addresses:
            if a.task:
                a.task.cancel()
        for t in list(self._tasks):
            t.cancel()
        for c in list(self.conns.values()):
            await c.ws.close()
        if self._runner:
            await self._runner.cleanup()

    def _restart(self) -> None:
        """Een andere sleutel of naam: alle verbindingen opnieuw, met de nieuwe gegevens."""
        for c in list(self.conns.values()):
            self._spawn(c.ws.close())
        for a in self.addresses:
            if a.task:
                a.task.cancel()
            self._start_client(a)

    def _start_client(self, a: Address) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # nog niet gestart: start() doet het
        a.task = loop.create_task(self._client(a))

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _accept(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=HEARTBEAT, max_msg_size=MAX_MESSAGE)
        await ws.prepare(request)
        key = self._key()
        if key is None or not self.me["id"]:
            await ws.close()
            return ws
        try:
            msg = await ws.receive(timeout=HANDSHAKE)
            if msg.type != aiohttp.WSMsgType.BINARY:
                raise LinkError("geen handdruk")
            pid, name, n_other = open_hello(key, msg.data, "hello")
            if pid == self.me["id"]:
                raise LinkError("de andere installatie heeft dezelfde naam als deze")
            n_me = os.urandom(16)
            await ws.send_bytes(seal_hello(key, "welcome", self.me["id"], self.me["name"], n_me))
            sess = Session(key, n_other, n_me, initiator=False)
        except (LinkError, asyncio.TimeoutError) as e:
            log.warning("koppeling vanaf %s geweigerd: %s", request.remote, e)
            await ws.close()
            return ws
        await self._serve(ws, sess, pid, name, initiator=pid)
        return ws

    async def _client(self, a: Address) -> None:
        delay = self.retry[0]
        host = f"[{a.host}]" if ":" in a.host else a.host
        while True:
            key = self._key()
            if key is None or not self.me["id"]:
                a.state, a.error = "wacht op een naam en een koppelsleutel", ""
                await asyncio.sleep(self.retry[0])
                continue
            a.state, a.error = "verbinden", ""
            try:
                async with aiohttp.ClientSession() as s:
                    ws = await asyncio.wait_for(s.ws_connect(f"http://{host}:{a.port}/link", heartbeat=HEARTBEAT, max_msg_size=MAX_MESSAGE), HANDSHAKE)
                    try:
                        n_me = os.urandom(16)
                        await ws.send_bytes(seal_hello(key, "hello", self.me["id"], self.me["name"], n_me))
                        msg = await ws.receive(timeout=HANDSHAKE)
                        if msg.type != aiohttp.WSMsgType.BINARY:
                            raise LinkError("geen antwoord: een andere koppelsleutel, of de andere installatie heeft nog geen naam")
                        pid, name, n_other = open_hello(key, msg.data, "welcome")
                        if pid == self.me["id"]:
                            raise LinkError("de andere installatie heeft dezelfde naam als deze")
                        a.peer, a.state, delay = pid, "verbonden", self.retry[0]
                        dup = await self._serve(ws, Session(key, n_me, n_other, initiator=True), pid, name, initiator=self.me["id"], addr=a)
                    finally:
                        await ws.close()
                if dup:
                    a.state, a.error = "verbonden via de verbinding van de andere kant", ""
                    delay = self.retry[1]
                else:
                    a.state = "verbroken"
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, LinkError) as e:
                a.state, a.error = "fout", str(e) or e.__class__.__name__
                log.debug("koppeling %s:%d: %s", a.host, a.port, a.error)
            self._poke()
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.retry[1])

    async def _serve(self, ws, sess: Session, pid: str, name: str, initiator: str, addr: Address | None = None) -> bool:
        """Eén verbinding na de handdruk. Geeft True als ze wijkt voor een andere verbinding met dezelfde installatie."""
        conn = Conn(ws, sess, pid, initiator)
        pref = min(self.me["id"], pid)
        old = self.conns.get(pid)
        if old is not None and not old.ws.closed:
            if old.initiator == pref and conn.initiator != pref:
                await self._send(conn, {"t": "dup"})
                return True
            await old.ws.close()
        self.conns[pid] = conn
        self._remember(pid, name)
        log.info("gekoppeld met %s (%s)", name, pid)
        self._poke()
        dup = False
        try:
            await self._send_want(conn)
            await self._push(conn)
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.BINARY:
                    if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
                    continue
                try:
                    m = sess.open(msg.data)
                except LinkError as e:
                    log.warning("%s: %s; verbinding gesloten", name, e)
                    break
                if m.get("t") == "dup":
                    dup = True
                    break
                await self._on_message(conn, m)
        finally:
            if self.conns.get(pid) is conn:
                del self.conns[pid]
                log.info("koppeling met %s weg", name)
                self._feed(pid)  # haar gangen in onze kringen: niet beschikbaar
                self._poke()
            for f in conn.futures.values():
                if not f.done():
                    f.set_exception(LinkError("verbinding weg"))
        return dup

    async def _send(self, conn: Conn, obj: dict) -> None:
        async with conn.lock:  # de teller moet in volgorde op de draad
            await conn.ws.send_bytes(conn.sess.seal(obj))

    async def _push(self, conn: Conn) -> None:
        """De catalogus aan iedereen; de stand alleen van wat de andere kant gekozen heeft. Telkens alleen als er iets veranderd is."""
        objs = self.export()
        catalog = json.dumps({k: {"kind": v["kind"], "name": v["name"], **({"busy": v["busy"]} if "busy" in v else {})} for k, v in objs.items()},
                             sort_keys=True, ensure_ascii=False)
        if catalog != conn.sent_catalog:
            conn.sent_catalog = catalog
            await self._send(conn, {"t": "catalog", "objects": json.loads(catalog)})
        text = json.dumps({k: v for k, v in objs.items() if k in conn.want}, sort_keys=True, ensure_ascii=False)
        if text != conn.sent:
            conn.sent = text
            await self._send(conn, {"t": "state", "objects": json.loads(text)})

    async def _on_message(self, conn: Conn, m: dict) -> None:
        t = m.get("t")
        if t in ("state", "catalog"):
            objs = m.get("objects")
            if not isinstance(objs, dict) or len(objs) > MAX_OBJECTS:
                return
            clean = {k: v for k, v in objs.items() if isinstance(k, str) and OBJ_RE.match(k) and isinstance(v, dict) and v.get("kind") in ("lamp", "tuya", "gang")}
            if t == "state":
                use = self.used(conn.peer)
                self.remote[conn.peer] = {k: v for k, v in clean.items() if k in use}
                self._feed(conn.peer)
            else:
                catalog = {k: {"kind": v["kind"], "name": str(v.get("name") or k)[:60], **({"busy": bool(v.get("busy"))} if v["kind"] == "gang" else {})}
                           for k, v in clean.items()}
                if catalog != self.known.get(conn.peer, {}).get("objects"):
                    self.known.setdefault(conn.peer, {"name": conn.peer})["objects"] = catalog
                    self._save()
            self._poke()
        elif t == "want":
            objs = m.get("objects")
            if isinstance(objs, list) and len(objs) <= MAX_OBJECTS:
                conn.want = {o for o in objs if isinstance(o, str) and OBJ_RE.match(o)}
                log.info("%s gebruikt %d %s van deze installatie", self.peer_name(conn.peer), len(conn.want), "toestel" if len(conn.want) == 1 else "toestellen")
                await self._push(conn)
        elif t == "cmd":
            err = self._execute(conn, m)
            await self._send(conn, {"t": "ack", "i": m.get("i"), "ok": err is None, "error": err or ""})
            await self._push(conn)  # de nieuwe stand meteen, niet pas bij de volgende tik
        elif t == "ack":
            f = conn.futures.get(m.get("i"))
            if f and not f.done():
                f.set_result(m)

    def _remember(self, pid: str, name: str) -> None:
        """Een installatie en haar naam onthouden: haar blokken blijven staan als ze offline is, ook na een herstart."""
        k = self.known.get(pid)
        if k is None or k.get("name") != name:
            self.known[pid] = {"objects": {}, **(k or {}), "name": name}
            self._save()

    def _poke(self) -> None:
        self.engine._emit("link", log=False)

    def tick(self) -> None:
        """Elke seconde: wat veranderd is naar elke verbonden installatie."""
        for c in list(self.conns.values()):
            if not c.ws.closed:
                self._spawn(self._push_safe(c))

    def _feed(self, pid: str) -> None:
        """De stand van de gekozen gangen van een installatie naar de kringen, zoals een stand uit HA (sync.py herkent een druk)."""
        up = pid in self.conns
        for obj in self.used(pid):
            if not obj.startswith("e:"):
                continue
            o = self.remote.get(pid, {}).get(obj) if up else None
            st = "unavailable" if not o or not o.get("online") or o.get("on") is None else ("on" if o["on"] else "off")
            ref = f"r:{pid}:{obj}"
            if self._fed.get(ref) != st:
                self._fed[ref] = st
                self.engine.ha_state(ref, st)

    def _on_engine(self, ev: dict) -> None:
        """Het model veranderd (bv. een remote gang net aan een kring gehangen): elk lid opnieuw zijn stand geven. Een lid dat al
        een stand had merkt niets; een nieuw lid neemt zo meteen de stand van de lamp over (sync._align)."""
        if ev.get("t") == "model":
            self._fed.clear()
            for pid in list(self.use):
                self._feed(pid)

    def _remote_call(self, ref: str, on: bool) -> bool:
        """Een kring zet een gang van een andere installatie: het bevel gaat naar de eigenaar. False zonder verbinding."""
        hit = split_ref(ref)
        return bool(hit and self.send(hit[0], hit[1], op="on" if on else "off"))

    async def _push_safe(self, c: Conn) -> None:
        try:
            await self._push(c)
        except (ConnectionError, RuntimeError) as e:
            log.debug("stand naar %s niet verstuurd: %s", c.peer, e)

    async def run(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(1.0)
