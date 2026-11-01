"""De NSPanels van het huis: configuratie, UDP naar het Berry-script op de panel, HA-standen en de twee relais.

Verkeer (UDP, tekst, één bericht per pakket; de panel luistert op PANEL_PORT, wij op HUB_PORT):

  hub -> panel   D|<tekst voor het scherm>     doorgeven aan de Nextion (bv. `pageType~cardGrid`)
                 P|<1|2>|<0|1>                 relais zetten
  panel -> hub   S|<tekst van het scherm>      bv. `event,buttonPress2,switch.x,OnOff,1`
                 H|{"p1":0,"p2":1,"t":24.7,"rssi":-61,"boot":false}   hartslag, elke 15 s en bij elke wijziging van een relais

Alleen pakketten van het IP-adres van de panel tellen. Dat is geen beveiliging tegen iemand op het LAN (Tasmota zelf heeft
op dit netwerk ook geen wachtwoord), wel tegen vergissingen.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import discovery as d
from .engine import Engine
from .habridge import HaBridge
from .panel import Panel, PanelError

log = logging.getLogger("kk.panelhub")

HUB_PORT = 47480
PANEL_PORT = 47481
STALE = 45.0  # s zonder hartslag: de panel is weg
PACE = 0.03  # s tussen twee pakketten naar een panel: de ontvangstbuffer van de ESP32 is klein
DEBOUNCE = 0.3
RESEND = 120.0  # s: de huidige pagina elke zoveel tijd opnieuw sturen, voor het geval er een pakket verloren ging
FORECAST_EVERY = 1800.0
MAX_PANELS = 8
SET_RE = re.compile(rf"^{d.PREFIX}/panel/([a-z0-9_]+)/relay/([12])/set$")
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def t_status(pid: str) -> str:
    return f"{d.PREFIX}/panel/{pid}/status"


def t_relay(pid: str, n: int) -> str:
    return f"{d.PREFIX}/panel/{pid}/relay/{n}/state"


def t_relay_set(pid: str, n: int) -> str:
    return f"{d.PREFIX}/panel/{pid}/relay/{n}/set"


def t_temp(pid: str) -> str:
    return f"{d.PREFIX}/panel/{pid}/temperature"


def t_rssi(pid: str) -> str:
    return f"{d.PREFIX}/panel/{pid}/rssi"


@dataclass
class Link:
    id: str
    name: str
    host: str
    yaml: str
    panel: Panel | None = None
    error: str = ""
    last_seen: float = 0.0
    online: bool = False
    greeted: bool = False
    relays: list[bool | None] = field(default_factory=lambda: [None, None])
    temp: float | None = None
    rssi: int | None = None
    outbox: "asyncio.Queue[bytes] | None" = None
    sender: "asyncio.Task | None" = None
    last_resend: float = 0.0
    last_poll: float = 0.0
    sent: int = 0
    received: int = 0


class _Env:
    """Wat een Panel van de buitenwereld ziet."""

    def __init__(self, hub: "PanelHub") -> None:
        self.hub = hub

    def state(self, entity: str):
        return self.hub.ha.states.get(entity)

    def template(self, text: str):
        return self.hub.ha.templates.get(text)

    def forecast(self, entity: str):
        return self.hub.forecasts.get(entity, [])

    def now(self):
        return self.hub.engine.local_now()

    def call(self, domain: str, service: str, data: dict) -> None:
        if not self.hub.ha.call_service(domain, service, data):
            log.warning("%s.%s niet verstuurd: geen verbinding met HA", domain, service)


class PanelHub(asyncio.DatagramProtocol):
    def __init__(self, path: Path, ha: HaBridge, engine: Engine, port: int = HUB_PORT, panel_port: int = PANEL_PORT,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.path, self.ha, self.engine = path, ha, engine
        self.port, self.panel_port, self._clock = port, panel_port, clock
        self.links: dict[str, Link] = {}
        self.forecasts: dict[str, list[dict]] = {}
        self.changed: Callable[[], None] | None = None  # voor de MQTT-brug: er is iets te publiceren
        self._tp: asyncio.DatagramTransport | None = None
        self._dirty: set[str] = set()
        self._flush_handle: asyncio.TimerHandle | None = None
        self._forecast_at = 0.0
        self._tasks: set[asyncio.Task] = set()
        self._env = _Env(self)
        self.icons = self._load_icons()
        self._load()
        ha.state_listeners.append(self._on_state)
        ha.template_listeners.append(self._on_template)

    # ------------------------------------------------------------ opslag

    def _load_icons(self) -> dict[str, str]:
        """Extra pictogrammen: {"naam": "\ue334"} in panel_icons.json naast de configuratie."""
        try:
            raw = json.loads((self.path.parent / "panel_icons.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.error("panel_icons.json onleesbaar: %s", e)
            return {}
        return {str(k).removeprefix("mdi:"): v for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, dict) else {}

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.error("%s onleesbaar, geen panelen geladen: %s", self.path, e)
            return
        for pid, v in (raw.get("panels") or {}).items():
            self.links[pid] = self._build(Link(pid, str(v.get("name") or pid), str(v.get("host") or ""), str(v.get("yaml") or "")))

    def _save(self) -> None:
        data = {"panels": {k: {"name": l.name, "host": l.host, "yaml": l.yaml} for k, l in self.links.items()}}
        tmp = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def _build(self, link: Link) -> Link:
        link.panel, link.error = None, ""
        if link.yaml.strip():
            try:
                link.panel = Panel(link.yaml, self._env, lambda text, l=link: self._send(l, b"D|" + text.encode("utf-8")), self.icons)
            except PanelError as e:
                link.error = str(e)
        link.greeted = False
        if link.panel:
            for t in link.panel.templates():
                self.ha.track_template(t)
            self._forecast_at = 0.0
        return link

    # ------------------------------------------------------------ beheer (web-API)

    def create(self, name: str, host: str, yaml: str) -> str:
        if len(self.links) >= MAX_PANELS:
            raise ValueError(f"maximaal {MAX_PANELS} panelen")
        base = re.sub(r"[^a-z0-9_]+", "_", (name or "nspanel").lower()).strip("_")[:30] or "nspanel"
        pid, n = base, 2
        while pid in self.links:
            pid, n = f"{base}_{n}", n + 1
        self.links[pid] = Link(pid, name.strip() or pid, "", "")
        try:
            self.update(pid, host=host, yaml=yaml)
        except ValueError:
            del self.links[pid]
            raise
        return pid

    def update(self, pid: str, name: str | None = None, host: str | None = None, yaml: str | None = None) -> None:
        link = self.links.get(pid)
        if link is None:
            raise KeyError(pid)
        host = link.host if host is None else host.strip()
        if host and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
            raise ValueError("geen geldig IP-adres of hostnaam")
        text = link.yaml if yaml is None else yaml
        if text.strip():  # een fout in de YAML wordt geweigerd, niet bewaard
            try:
                Panel(text, self._env, lambda _t: None)
            except PanelError as e:
                raise ValueError(str(e)) from e
        if name is not None:
            link.name = name.strip() or link.name
        reset = host != link.host or text != link.yaml
        link.host, link.yaml = host, text
        if reset:
            self._build(link)
        self._save()
        self._poke()

    def remove(self, pid: str) -> bool:
        link = self.links.pop(pid, None)
        if link is None:
            return False
        if link.sender:
            link.sender.cancel()
        self._save()
        self._poke()
        return True

    def set_relay(self, pid: str, n: int, on: bool) -> bool:
        link = self.links.get(pid)
        if link is None or n not in (1, 2) or not link.host:
            return False
        self._send(link, f"P|{n}|{1 if on else 0}".encode())
        return True

    def status(self) -> list[dict]:
        return [{
            "id": l.id, "name": l.name, "host": l.host, "yaml": l.yaml, "online": l.online, "error": l.error, "greeted": l.greeted,
            "last_seen": round(self._clock() - l.last_seen) if l.last_seen else None, "relays": l.relays, "temperature": l.temp,
            "rssi": l.rssi, "sent": l.sent, "received": l.received,
        } for l in self.links.values()]

    def _poke(self) -> None:
        if self.changed:
            self.changed()
        self.engine._emit("panel", log=False)

    # ------------------------------------------------------------ UDP

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._tp, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", self.port))
        log.info("panelen: UDP :%d", self.port)

    def close(self) -> None:
        for l in self.links.values():
            if l.sender:
                l.sender.cancel()
        for t in self._tasks:
            t.cancel()
        if self._tp:
            self._tp.close()

    def connection_made(self, transport) -> None:  # pragma: no cover - asyncio
        self._tp = transport

    def datagram_received(self, data: bytes, addr) -> None:
        link = next((l for l in self.links.values() if l.host and self._same_host(l.host, addr[0])), None)
        if link is None:
            return
        link.received += 1
        text = data.decode("utf-8", "replace")
        tag, _, body = text.partition("|")
        if tag == "H":
            self._heartbeat(link, body)
        elif tag == "S":
            link.last_seen = self._clock()
            if link.panel:
                log.debug("%s <- %s", link.id, body)
                link.panel.on_screen(body)

    @staticmethod
    def _same_host(host: str, ip: str) -> bool:
        if host == ip:
            return True
        try:
            import socket

            return ip in {a[4][0] for a in socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)}
        except OSError:
            return False

    def _send(self, link: Link, payload: bytes) -> None:
        if not link.host or self._tp is None:
            return
        if link.outbox is None:
            link.outbox = asyncio.Queue(maxsize=200)
            link.sender = asyncio.get_running_loop().create_task(self._drain(link))
        try:
            link.outbox.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("%s: wachtrij naar de panel vol, bericht overgeslagen", link.id)

    async def _drain(self, link: Link) -> None:
        while True:
            payload = await link.outbox.get()
            if self._tp and link.host:
                self._tp.sendto(payload, (link.host, self.panel_port))
                link.sent += 1
            await asyncio.sleep(PACE)

    # ------------------------------------------------------------ hartslag en toestand

    def _heartbeat(self, link: Link, body: str) -> None:
        try:
            h = json.loads(body)
        except ValueError:
            return
        was = (link.online, tuple(link.relays), link.temp, link.rssi)
        link.last_seen = self._clock()
        link.online = True
        link.relays = [None if h.get("p1") is None else bool(h["p1"]), None if h.get("p2") is None else bool(h["p2"])]
        link.temp = h.get("t") if isinstance(h.get("t"), (int, float)) else None
        link.rssi = h.get("rssi") if isinstance(h.get("rssi"), int) else None
        if h.get("boot"):  # de panel is net opgestart: het scherm weet niets meer
            link.greeted = False
        if was != (link.online, tuple(link.relays), link.temp, link.rssi):
            self._poke()

    # ------------------------------------------------------------ HA -> paneel

    def _on_state(self, entity: str | None) -> None:
        for l in self.links.values():
            if l.panel and l.greeted and (entity is None or entity in l.panel.entities()):
                self._dirty.add(l.id)
        self._schedule()

    def _on_template(self, _text: str) -> None:
        self._dirty.update(l.id for l in self.links.values() if l.panel and l.greeted)
        self._schedule()

    def _schedule(self) -> None:
        if self._dirty and self._flush_handle is None:
            self._flush_handle = asyncio.get_running_loop().call_later(DEBOUNCE, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        dirty, self._dirty = self._dirty, set()
        for pid in dirty:
            l = self.links.get(pid)
            if l and l.panel and l.greeted:
                l.panel.refresh()

    # ------------------------------------------------------------ elke seconde

    def tick(self) -> None:
        now = self._clock()
        changed = False
        for l in self.links.values():
            if l.online and now - l.last_seen > STALE:
                l.online, l.greeted, changed = False, False, True
                log.warning("panel %s reageert niet meer", l.name)
            if not l.online and l.host and now - l.last_poll >= 5.0:
                l.last_poll = now  # de panel mag direct antwoorden i.p.v. op zijn volgende hartslag te wachten
                self._send(l, b"Q|")
            if l.panel is None or not l.online:
                continue
            if not l.greeted and self.ha.connected:
                l.greeted, l.last_resend = True, now
                for t in l.panel.templates():
                    self.ha.track_template(t)
                l.panel.startup()
                log.info("panel %s: scherm gestart", l.name)
            elif l.greeted:
                l.panel.tick()
                if now - l.last_resend > RESEND:
                    l.last_resend = now
                    l.panel.refresh(force=True)
        if changed:
            self._poke()
        if self.ha.connected and now >= self._forecast_at and any(l.panel for l in self.links.values()):
            self._forecast_at = now + FORECAST_EVERY
            self._spawn(self._fetch_forecasts())

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _fetch_forecasts(self) -> None:
        for e in sorted({e for l in self.links.values() if l.panel for e in l.panel.forecast_entities()}):
            fc = await self.ha.forecast(e)
            if fc:
                self.forecasts[e] = fc
                self._on_template("")

    async def run(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------ MQTT: de relais en de temperatuur als objecten in HA

    @property
    def filters(self) -> list[str]:
        return [f"{d.PREFIX}/panel/+/relay/+/set"]

    def discovery(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for l in self.links.values():
            dev = {"identifiers": [f"kk_panel_{l.id}"], "name": l.name, "manufacturer": "Sonoff", "model": "NSPanel (Huisconfig)"}
            base = {"device": dev, "has_entity_name": True,
                    "availability": [{"topic": d.BRIDGE_STATUS}, {"topic": t_status(l.id)}], "availability_mode": "all"}
            for n in (1, 2):
                oid = f"kk_panel_{l.id}_relay{n}"
                out[f"{d.DISCOVERY}/switch/{oid}/config"] = {**base, "name": f"Relais {n}", "unique_id": oid, "command_topic": t_relay_set(l.id, n),
                                                              "state_topic": t_relay(l.id, n), "payload_on": "ON", "payload_off": "OFF"}
            oid = f"kk_panel_{l.id}_temp"
            out[f"{d.DISCOVERY}/sensor/{oid}/config"] = {**base, "name": "Temperatuur", "unique_id": oid, "state_topic": t_temp(l.id),
                                                          "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}
            oid = f"kk_panel_{l.id}_rssi"
            out[f"{d.DISCOVERY}/sensor/{oid}/config"] = {**base, "name": "Wifi-signaal", "unique_id": oid, "state_topic": t_rssi(l.id),
                                                          "device_class": "signal_strength", "unit_of_measurement": "dBm", "entity_category": "diagnostic",
                                                          "enabled_by_default": False}
        return out

    def states(self) -> dict[str, tuple[str, bool]]:
        out: dict[str, tuple[str, bool]] = {}
        for l in self.links.values():
            out[t_status(l.id)] = ("online" if l.online else "offline", True)
            for n, on in enumerate(l.relays, 1):
                if on is not None:
                    out[t_relay(l.id, n)] = ("ON" if on else "OFF", True)
            if l.temp is not None:
                out[t_temp(l.id)] = (f"{l.temp:.1f}", True)
            if l.rssi is not None:
                out[t_rssi(l.id)] = (str(l.rssi), True)
        return out

    def command(self, topic: str, payload: str) -> bool:
        m = SET_RE.match(topic)
        if not m:
            return False
        up = payload.upper()
        if up in ("ON", "OFF") and not self.set_relay(m.group(1), int(m.group(2)), up == "ON"):
            log.warning("commando voor onbekend paneel of relais: %s", topic)
        return True
