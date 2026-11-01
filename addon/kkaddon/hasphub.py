"""De schermen met openHASP-firmware: de hub hangt aan de MQTT-brug en aan de HA-koppeling. Vervangt de pyscript-koppeling.

Een scherm komt online (LWT, retained):
  1. vraag het merkteken op (`p0b250.text`). Klopt het met de configuratie, dan staat alles er nog: alleen de kleuren bijwerken.
     Zo laat een herstart van de add-on de schermen met rust.
  2. geen of een ander antwoord: scherm wissen en het ontwerp sturen, met het merkteken als laatste.
De status komt uit de LWT: de broker meldt zelf een scherm dat wegvalt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from . import discovery as d
from .engine import Engine
from .habridge import HaBridge
from .hasp import MARKER, Screen, chunks

log = logging.getLogger("kk.hasp")

QUERY_TIMEOUT = 3.0  # s: een scherm zonder ons ontwerp antwoordt niet op de vraag naar het merkteken
RESEND = 120.0  # s: de kleuren opnieuw sturen, voor het geval er een bericht verloren ging
TOPIC_RE = re.compile(r"^hasp/([^/]+)/(LWT|state/(.+))$")
BUTTON_RE = re.compile(r"^p\d+b\d+$")


def t_online(sid: str) -> str:
    return f"{d.PREFIX}/screen/{sid}/online"


@dataclass
class Link:
    id: str
    name: str
    plate: str
    pages: list
    timeout: int
    screen: Screen | None = None
    online: bool = False
    phase: str = "idle"  # idle, query, push, ready
    query_at: float = 0.0
    push: "asyncio.Task | None" = None
    uptime: str | None = None
    last_resend: float = 0.0
    received: int = 0


class _Env:
    def __init__(self, hub: "HaspHub") -> None:
        self.hub = hub

    def state(self, entity: str):
        return self.hub.ha.states.get(entity)

    def call(self, domain: str, service: str, data: dict) -> None:
        if not self.hub.ha.call_service(domain, service, data):
            log.warning("%s.%s niet verstuurd: geen verbinding met HA", domain, service)


class HaspHub:
    def __init__(self, ha: HaBridge, engine: Engine, clock: Callable[[], float] = time.monotonic, settle: tuple[float, float, float] = (1.0, 3.0, 0.05)) -> None:
        self.ha, self.engine, self._clock = ha, engine, clock
        self.settle = settle  # s: na `idle off`, na `clearpage`, tussen twee stukken ontwerp
        self.publish: Callable[[str, str], bool] = lambda _t, _p: False  # de MQTT-brug
        self.changed: Callable[[], None] | None = None
        self.seen: dict[str, bool] = {}  # elk scherm op de broker: een retained LWT komt maar één keer per verbinding binnen
        self.links: dict[str, Link] = {}
        self._env = _Env(self)
        self._sync()
        engine.model.on_change(self._sync)
        engine.screen_view = self.status
        ha.state_listeners.append(self._on_state)

    # ------------------------------------------------------------ de configuratie staat in het model

    def _sync(self) -> None:
        screens = self.engine.model.screens
        changed = False
        for sid in [s for s in self.links if s not in screens]:
            self._cancel(self.links.pop(sid))
            changed = True
        for sid, s in screens.items():
            link = self.links.get(sid)
            if link is None:
                link = self.links[sid] = Link(sid, s["name"], "", [], s["timeout"])
                changed = True
            if link.name != s["name"] or link.timeout != s["timeout"]:
                link.name, link.timeout, changed = s["name"], s["timeout"], True
                if link.screen:
                    link.screen.timeout = s["timeout"]
            if (link.plate, link.pages) != (s["plate"], s["pages"]):
                self._rebuild(link, s["plate"], s["pages"])
                changed = True
        if changed:
            self._poke()

    def _rebuild(self, link: Link, plate: str, pages: list) -> None:
        self._cancel(link)
        same_plate = link.plate == plate
        link.plate, link.pages = plate, pages
        link.screen = Screen(pages, link.timeout, self._env, lambda cmd, payload, l=link: self._cmd(l, cmd, payload)) if plate else None
        link.online = bool(plate) and self.seen.get(plate, False)
        link.phase = "idle"
        if link.online:
            if same_plate:
                self._start_push(link)  # we weten al dat het ontwerp verschilt
            else:
                self._query(link)

    def _cancel(self, link: Link) -> None:
        if link.push:
            link.push.cancel()
            link.push = None

    def _poke(self) -> None:
        if self.changed:
            self.changed()
        self.engine._emit("screen", log=False)

    def status(self) -> list[dict]:
        plates = sorted(self.seen)
        out = []
        for l in self.links.values():
            missing = sorted(e for e in l.screen.entities() if e not in self.ha.states) if l.screen and self.ha.connected else []
            out.append({"id": l.id, "name": l.name, "plate": l.plate, "timeout": l.timeout, "pages": l.pages, "online": l.online, "phase": l.phase,
                        "missing": missing, "uptime": l.uptime, "received": l.received, "plates": plates})
        return out

    # ------------------------------------------------------------ naar het scherm

    def _cmd(self, link: Link, cmd: str, payload: str) -> bool:
        return self.publish(f"hasp/{link.plate}/command/{cmd}", payload)

    def _query(self, link: Link) -> None:
        self._cancel(link)
        link.phase, link.query_at = "query", self._clock()
        if not self._cmd(link, MARKER + ".text", ""):
            link.phase = "idle"  # geen MQTT: bij het opnieuw verbinden komt de LWT weer binnen

    def _start_push(self, link: Link) -> None:
        self._cancel(link)
        link.phase = "push"
        link.push = asyncio.get_running_loop().create_task(self._push(link))

    async def _push(self, link: Link) -> None:
        idle, clear, pace = self.settle
        screen = link.screen
        log.info("scherm %s: ontwerp sturen", link.name)
        ok = self._cmd(link, "idle", "off")
        await asyncio.sleep(idle)
        ok = self._cmd(link, "clearpage", "all") and ok
        await asyncio.sleep(clear)
        for part in chunks(screen.design()):
            ok = self._cmd(link, "jsonl", part) and ok
            await asyncio.sleep(pace)
        if not ok:
            log.warning("scherm %s: ontwerp niet volledig verstuurd", link.name)
            link.phase = "idle"
            return
        self._cmd(link, "page", "1")
        self._ready(link)

    def _ready(self, link: Link) -> None:
        link.phase, link.last_resend, link.push = "ready", self._clock(), None
        link.screen.ready(self._clock())
        self._poke()

    # ------------------------------------------------------------ HA -> scherm

    def _on_state(self, entity: str | None) -> None:
        for l in self.links.values():
            if l.phase == "ready" and (entity is None or entity in l.screen.entities()):
                l.screen.sync()
        if entity is None:
            self._poke()  # alle standen vers: de lijst met ontbrekende entiteiten kan veranderd zijn

    # ------------------------------------------------------------ elke seconde

    def tick(self) -> None:
        now = self._clock()
        for l in self.links.values():
            if l.phase == "query" and now - l.query_at >= QUERY_TIMEOUT:
                self._start_push(l)
            elif l.phase == "ready":
                l.screen.tick(now)
                if now - l.last_resend >= RESEND:
                    l.last_resend = now
                    l.screen.sync(force=True)

    async def run(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(1.0)

    def close(self) -> None:
        for l in self.links.values():
            self._cancel(l)

    # ------------------------------------------------------------ MQTT

    @property
    def filters(self) -> list[str]:
        return ["hasp/+/LWT", "hasp/+/state/#"]

    def command(self, topic: str, payload: str) -> bool:
        m = TOPIC_RE.match(topic)
        if not m:
            return False
        plate = m.group(1)
        if m.group(2) == "LWT":
            self._lwt(plate, payload == "online")
            return True
        new = plate not in self.seen
        self.seen[plate] = True
        link = next((l for l in self.links.values() if l.plate == plate), None)
        if link is None:
            if new:
                self._poke()
            return True
        link.received += 1
        if not link.online:  # een scherm dat iets zegt is er, ook als we zijn LWT nog niet zagen
            self._lwt(plate, True)
        self._state(link, m.group(3), payload)
        return True

    def _lwt(self, plate: str, online: bool) -> None:
        self.seen[plate] = online
        link = next((l for l in self.links.values() if l.plate == plate), None)
        if link is None:
            self._poke()
            return
        link.online = online
        if online:
            if link.phase != "push":  # soms komt "online" vlak na elkaar: een lopend ontwerp gewoon afmaken
                self._query(link)
        else:
            self._cancel(link)
            link.phase = "idle"
            log.warning("scherm %s is weg", link.name)
        self._poke()

    def _state(self, link: Link, obj: str, payload: str) -> None:
        now = self._clock()
        if obj == MARKER:
            if link.phase == "query":
                try:
                    text = json.loads(payload).get("text")
                except (ValueError, AttributeError):
                    text = None
                if text == link.screen.hash:
                    log.info("scherm %s: ontwerp staat er nog", link.name)
                    self._cmd(link, "backlight", "")  # is het scherm nu donker? het antwoord komt hieronder binnen
                    self._ready(link)
                else:
                    self._start_push(link)
        elif obj == "sensors":
            try:
                uptime = json.loads(payload).get("uptime")  # zo gemeten op de schermen: "22T23:03:41"
            except (ValueError, AttributeError):
                uptime = None
            link.uptime = uptime if isinstance(uptime, str) else None
        elif link.phase != "ready":
            return
        elif obj == "backlight":
            link.screen.on_backlight(payload, now)
        elif BUTTON_RE.match(obj):
            link.screen.on_event(obj, payload, now)

    # ------------------------------------------------------------ de status als object in HA

    def discovery(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for l in self.links.values():
            oid = f"kk_screen_{l.id}_online"
            out[f"{d.DISCOVERY}/binary_sensor/{oid}/config"] = {
                "device": {"identifiers": [f"kk_screen_{l.id}"], "name": l.name, "manufacturer": "openHASP", "model": "Scherm (Huisconfig)"},
                "has_entity_name": True, "name": "Verbinding", "unique_id": oid, "state_topic": t_online(l.id), "payload_on": "ON", "payload_off": "OFF",
                "device_class": "connectivity", "entity_category": "diagnostic", "availability": [{"topic": d.BRIDGE_STATUS}], "default_entity_id": f"binary_sensor.{oid}"}
        return out

    def states(self) -> dict[str, tuple[str, bool]]:
        return {t_online(l.id): ("ON" if l.online else "OFF", True) for l in self.links.values()}
