"""Koppelingen met andere Huisconfig-installaties (een ander subnet, met een eigen HA en add-on).

Elke installatie
  * luistert op een eigen TCP-poort (`link_port`, standaard 47490) en verbindt met de adressen die je opgeeft. Eén kant opgeven
    volstaat: de verbinding werkt in beide richtingen. Geven beide kanten elkaar op, dan blijft de verbinding over die is
    opgezet door de installatie met de kleinste id (aan beide kanten dezelfde regel), de andere wordt gesloten.
  * deelt haar lampen (kringen en lichten op het netwerk) en Tuya-toestellen, met hun stand, elke keer dat er iets verandert.
  * voert bevelen voor haar **eigen** toestellen uit: de eigenaar beslist, met dezelfde regels als in de eigen UI (een lamp met
    een aan/uit-ingang laat zich niet schakelen, een Tuya-datapunt alleen binnen zijn datamodel).

Wat een andere installatie deelt, verschijnt hier als alleen-lezen blok (`r:<installatie>:<object>`), te bedienen en aan een
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
from .link import ID_RE, MAX_MESSAGE, OBJ_RE, LinkError, Session, derive, open_hello, seal_hello
from .model import ModelError

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
        self.me = {"id": "", "name": ""}
        self.key_text = ""
        self.addresses: list[Address] = []
        self.known: dict[str, dict] = {}  # installatie -> {name, objects: {obj: {kind, name}}}: blijft na een herstart
        self.remote: dict[str, dict] = {}  # installatie -> {obj: stand}, alleen zolang verbonden
        self.conns: dict[str, Conn] = {}
        self.listening, self.error = False, ""
        self._runner: web.AppRunner | None = None
        self._tasks: set[asyncio.Task] = set()
        self._seq = 0
        self._load()
        engine.link_view = self.status

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

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        d = {**self.me, "key": self.key_text, "peers": [{"host": a.host, "port": a.port} for a in self.addresses], "known": self.known}
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
        self._save()
        self._poke()
        return True

    # ------------------------------------------------------------ voor de pagina en de kaarten

    def status(self) -> dict:
        remote = {}
        for pid in sorted(set(self.known) | set(self.remote)):
            up = pid in self.conns
            objs = {o: {**k, "online": False} for o, k in (self.known.get(pid, {}).get("objects") or {}).items()}
            if up:
                objs.update(self.remote.get(pid, {}))
            remote[pid] = {"name": self.known.get(pid, {}).get("name") or pid, "online": up, "objects": objs}
        return {"me": {**self.me, "port": self.port, "has_key": bool(self.key_text), "listening": self.listening, "error": self.error},
                "peers": [{"host": a.host, "port": a.port, "state": a.state, "error": a.error, "peer": a.peer} for a in self.addresses],
                "remote": remote}

    def is_up(self, pid: str) -> bool:
        return pid in self.conns

    def object(self, pid: str, obj: str) -> dict | None:
        """De stand van een object van een andere installatie; offline (met de laatst bekende naam) als de verbinding weg is."""
        if pid in self.conns and obj in self.remote.get(pid, {}):
            return self.remote[pid][obj]
        k = (self.known.get(pid, {}).get("objects") or {}).get(obj)
        return {**k, "online": False} if k else None

    def peer_name(self, pid: str) -> str:
        return self.known.get(pid, {}).get("name") or pid

    def send(self, pid: str, obj: str, **cmd) -> bool:
        """Een bevel zonder op het antwoord te wachten (een druk op een paneel). False als er geen verbinding is."""
        if pid not in self.conns:
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
        return out

    def _execute(self, peer: str, m: dict) -> str | None:
        """Een bevel van een andere installatie voor een eigen toestel. Geeft de reden van een weigering, of None."""
        obj, op, e = m.get("obj"), m.get("op"), self.engine
        if not isinstance(obj, str) or not OBJ_RE.match(obj):
            return "ongeldig bevel"
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
                self._poke()
            for f in conn.futures.values():
                if not f.done():
                    f.set_exception(LinkError("verbinding weg"))
        return dup

    async def _send(self, conn: Conn, obj: dict) -> None:
        async with conn.lock:  # de teller moet in volgorde op de draad
            await conn.ws.send_bytes(conn.sess.seal(obj))

    async def _push(self, conn: Conn) -> None:
        text = json.dumps(self.export(), sort_keys=True, ensure_ascii=False)
        if text != conn.sent:
            conn.sent = text
            await self._send(conn, {"t": "state", "objects": json.loads(text)})

    async def _on_message(self, conn: Conn, m: dict) -> None:
        t = m.get("t")
        if t == "state":
            objs = m.get("objects")
            if not isinstance(objs, dict) or len(objs) > MAX_OBJECTS:
                return
            clean = {k: v for k, v in objs.items() if isinstance(k, str) and OBJ_RE.match(k) and isinstance(v, dict) and v.get("kind") in ("lamp", "tuya")}
            self.remote[conn.peer] = clean
            catalog = {k: {"kind": v["kind"], "name": str(v.get("name") or k)[:60]} for k, v in clean.items()}
            if catalog != self.known.get(conn.peer, {}).get("objects"):
                self.known.setdefault(conn.peer, {"name": conn.peer})["objects"] = catalog
                self._save()
            self._poke()
        elif t == "cmd":
            err = self._execute(conn.peer, m)
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

    async def _push_safe(self, c: Conn) -> None:
        try:
            await self._push(c)
        except (ConnectionError, RuntimeError) as e:
            log.debug("stand naar %s niet verstuurd: %s", c.peer, e)

    async def run(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(1.0)
