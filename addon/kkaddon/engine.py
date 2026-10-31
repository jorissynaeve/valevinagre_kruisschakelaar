"""De add-on als peer in het ESP-NOW/UDP-netwerk: kent de staat van alle nodes, schakelt lichten,
leert schakelaars aan lichten en houdt de configuratie van de nodes gelijk aan het model.

Synchroon en klok-gedreven (geen asyncio, geen sockets): de runner roept `on_datagram` en `tick` aan,
tests sturen de tijd zelf. De add-on forwardt niets: bridges praten onderling over de LAN
(DESIGN.md §2); de peer luistert, zendt en houdt een eigen overzicht bij.
"""
from __future__ import annotations

import heapq
import json
import logging
import os
import random
import tempfile
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import proto
from .logic import disable_ref, evaluate, sun_times
from .model import Model, ModelError, timer_active
from .proto import CF_BRIDGE, DEFAULT_TTL, LINK_LOCAL, T, Frame, Mode
from .replay import ReplayTable
from .sync import EntityGroups
from .virtual import Virtuals

log = logging.getLogger("kk.engine")

PEER_ID_MIN = 0xF000  # ids vanaf hier zijn voor infrastructuur (add-on), nodes krijgen 1..0xEFFF


@dataclass
class Tuning:
    copies: int = 2  # elk frame 2x op de LAN (de ontvangende bridge dedupliceert)
    copy_gap: tuple[float, float] = (0.006, 0.012)
    cmd_retry: tuple[float, ...] = (0.30, 0.60, 1.20, 2.40)  # wachttijd na poging 1, 2, ...
    learn_refresh: float = 25.0  # nodes laten het leren na 120 s los: vernieuwen
    learn_idle: float = 300.0  # sessie stopt vanzelf zonder activiteit
    offline_after: float = 240.0  # > 3x de langste heartbeat-interval (80 s)
    cfg_retry: tuple[float, ...] = (1.5, 3.0, 6.0)  # wachttijd na poging 1, 2, ... (daarna: mislukt)
    cfg_spacing: float = 0.04  # tussen frames van een push (de flood krijgt zo ruimte)
    cfg_grace: float = 90.0  # na een push: oudere heartbeats met de oude hash negeren
    auto_push_base: float = 30.0
    auto_push_max: float = 600.0
    log_size: int = 400
    maintenance: float = 10.0  # offline-detectie en bewaren


@dataclass
class RelayState:
    on: bool | None = None
    ver: int = 0
    ver_known: bool = False


@dataclass
class Push:
    content: dict[tuple[str, int], tuple]  # ("t", relais) of ("f", indicator) -> wat we sturen
    acked: set = field(default_factory=set)
    tries: int = 0
    auto: bool = False


@dataclass
class NodeInfo:
    id: int
    first_seen: float = 0.0
    last_seen: float = 0.0
    online: bool = False
    fw: int = 0
    flags: int = 0
    boot: int | None = None
    nbtn: int | None = None
    nind: int | None = None
    cfg_hash: int | None = None
    relays: list[RelayState] = field(default_factory=list)
    cfg_status: str = "unknown"  # ok | applied | pushing | drift | failed | unmanaged | unknown
    push: Push | None = None
    sent: dict = field(default_factory=dict)  # item -> laatst bevestigde inhoud
    next_auto_push: float = 0.0
    backoff: float = 0.0
    resync: bool = False  # er kwam een wijziging binnen tijdens een lopende push
    virtual: bool = False  # extern apparaat dat de add-on vertegenwoordigt (zie virtual.py)

    @property
    def bridge(self) -> bool:
        return bool(self.flags & CF_BRIDGE)


@dataclass
class PendingCmd:
    target: bool
    started: float
    tries: int = 1


@dataclass
class Learn:
    target: tuple[int, int] | None
    started: float
    last_activity: float
    detected: "OrderedDict[tuple[int, int], dict]" = field(default_factory=OrderedDict)


def fw_str(fw: int) -> str:
    return f"{fw >> 8}.{fw & 0xFF}"


class Engine:
    def __init__(
        self,
        key: bytes,
        model: Model,
        tx: Callable[[bytes], None],
        *,
        peer_id: int = PEER_ID_MIN,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        tuning: Tuning | None = None,
        state_path: str | os.PathLike | None = None,
    ) -> None:
        if len(key) != proto.KEY_LEN:
            raise ValueError("netwerksleutel moet 32 bytes zijn")
        if not PEER_ID_MIN <= peer_id < proto.ID_BROADCAST:
            raise ValueError("peer_id moet tussen 0xF000 en 0xFFFE liggen")
        self.key, self.model, self._tx, self.id = key, model, tx, peer_id
        self._clock, self._wall = clock, wall
        self._rng = rng or random.Random()
        self.t = tuning or Tuning()
        self.state_path = Path(state_path) if state_path else None

        self.nodes: dict[int, NodeInfo] = {}
        self.log: deque[dict] = deque(maxlen=self.t.log_size)
        self.stats = {"rx": 0, "tx": 0, "dups": 0, "bad_auth": 0, "ignored": 0}
        self.pending_cmds: dict[tuple[int, int], PendingCmd] = {}
        self.learn: Learn | None = None
        self.relay_cb: Callable[[bytes, Any], None] | None = None  # hub: frame doorgeven aan andere peers
        self.ha_call: Callable[[str, bool], bool] | None = None  # HA-entiteit aan/uit zetten (habridge.py); False = mislukt

        self._replay = ReplayTable()
        self._btn_seen: OrderedDict[tuple[int, int, int, int], None] = OrderedDict()
        self._subs: list[Callable[[dict], None]] = []
        self._timers: list[tuple[float, int, Callable[[], None]]] = []
        self._seq = 0
        self._event_id = 0
        self._dirty = False
        self._ctr = 0
        self._vctr: dict[int, int] = {}  # frame-teller per virtuele node
        self._ui_evt = 0
        self._vver: dict[str, int] = {}  # bewaarde ver-tellers van externe gangen
        self.boot = 0
        self.location: tuple[float, float] | None = None  # breedte en lengte uit HA, voor de zon

        self._load_state()
        self.virt = Virtuals(self)
        self.virt.rebuild()
        self.groups = EntityGroups(self)
        self.groups.rebuild()
        self.model.on_change(self._model_changed)
        self._at(self.t.maintenance, self._maintenance)

    # ------------------------------------------------------------ klok en timers

    def now(self) -> float:
        return self._clock()

    def _at(self, delay: float, fn: Callable[[], None]) -> None:
        self._seq += 1
        heapq.heappush(self._timers, (self._clock() + delay, self._seq, fn))

    def tick(self) -> None:
        now = self._clock()
        while self._timers and self._timers[0][0] <= now:
            _, _, fn = heapq.heappop(self._timers)
            fn()

    # ------------------------------------------------------------ events

    def subscribe(self, cb: Callable[[dict], None]) -> None:
        self._subs.append(cb)

    def _emit(self, kind: str, log: bool = True, **kw: Any) -> dict:
        self._event_id += 1
        entry = {"id": self._event_id, "ts": self._wall(), "t": kind, **kw}
        if log:
            self.log.append(entry)
        for cb in list(self._subs):
            cb(entry)
        return entry

    # ------------------------------------------------------------ van HA (habridge.py)

    def ha_entities(self) -> set[str]:
        """Alle HA-entiteiten die we volgen: gangen van externe apparaten en leden van kringen."""
        return self.virt.entities() | self.groups.entities()

    def ha_state(self, entity: str, st: str | None) -> None:
        if entity in self.virt.entities():
            self.virt.ha_event(entity, st)
        self.groups.ha_event(entity, st)

    def ha_snapshot(self, states: dict[str, str]) -> None:
        self.virt.ha_sync({k: v for k, v in states.items() if k in self.virt.entities()})
        self.groups.ha_sync(states)

    # ------------------------------------------------------------ tijd van het huis (timers)

    def set_tz(self, name: str | None) -> None:
        """De tijdzone van Home Assistant. De container kan op UTC staan; dan zou 3u 's nachts op het verkeerde uur vallen."""
        try:
            self.tz = ZoneInfo(name) if name else None
        except (ZoneInfoNotFoundError, ValueError, OSError):
            log.warning("onbekende tijdzone %r: de timers gebruiken de tijd van de container", name)
            self.tz = None

    def local_now(self) -> datetime:
        return datetime.fromtimestamp(self._wall(), getattr(self, "tz", None))

    def timer_on(self, tid: str) -> bool:
        t = self.model.timers.get(tid)
        if t is None:
            return False
        now = self.local_now()
        return timer_active(t, now.weekday(), now.hour * 60 + now.minute)

    def set_location(self, lat, lon) -> None:
        ok = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (lat, lon))
        self.location = (float(lat), float(lon)) if ok else None
        if not ok:
            log.warning("geen locatie uit HA: de zonsopgang en -ondergang blijven onbekend")

    def sun_today(self) -> tuple[int, int] | None:
        if self.location is None:
            return None
        return sun_times(self.local_now().date(), *self.location, getattr(self, "tz", None))

    def module_values(self) -> dict:
        now = self.local_now()
        return evaluate(self.model.modules, lambda t: self.timer_on(t) if t in self.model.timers else None, now.hour * 60 + now.minute, self.sun_today())

    def source_on(self, disable: str) -> bool:
        """Geeft de bron op een disable-ingang nu 1? Onbekend telt als 0: een lamp blijft dan gewoon te bedienen."""
        ref = disable_ref(disable)
        return self.timer_on(ref[2:]) if ref.startswith("t:") else self.module_values().get(ref[2:]) is True

    def _model_changed(self) -> None:
        self.virt.rebuild()
        self.groups.rebuild()
        self._emit("model", log=False, rev=self.model.rev)

    # ------------------------------------------------------------ zenden

    def _frame(self, ftype: int, payload: bytes, src: int | None = None) -> bytes:
        """Seal een frame als de add-on zelf (src=None) of als een virtuele node."""
        src = self.id if src is None else src
        ctr = self._ctr if src == self.id else self._vctr.get(src, 0)
        if ctr == 0xFFFF:  # nonce-ruimte van deze boot op: nieuwe boot voor alle bronnen
            self.boot = (self.boot + 1) & 0xFFFF
            self._ctr, ctr = 0, 0
            self._vctr = {}
            self._save_state()
        f = Frame(ftype, DEFAULT_TTL, src, self.boot, ctr, payload)
        if src == self.id:
            self._ctr = ctr + 1
        else:
            self._vctr[src] = ctr + 1
        raw = proto.seal(self.key, f)
        self._replay.accept(f.src, f.key)  # onze eigen echo (broadcast) negeren we later
        return raw

    def send_as(self, src: int, ftype: int, payload: bytes, copies: int | None = None) -> None:
        """Zenden namens een virtuele node (een extern apparaat)."""
        self.send(ftype, payload, copies, src)

    def send(self, ftype: int, payload: bytes, copies: int | None = None, src: int | None = None) -> None:
        raw = self._frame(ftype, payload, src)
        n = self.t.copies if copies is None else copies
        for i in range(n):
            if i == 0:
                self._tx(raw)
                self.stats["tx"] += 1
            else:
                self._at(self._rng.uniform(*self.t.copy_gap) * i, lambda r=raw: self._send_raw(r))

    def _send_raw(self, raw: bytes) -> None:
        self._tx(raw)
        self.stats["tx"] += 1

    # ------------------------------------------------------------ ontvangen

    def on_datagram(self, data: bytes, addr: Any = None) -> None:
        f = proto.open_frame(self.key, data)
        if f is None:
            self.stats["bad_auth"] += 1
            return
        if f.src == self.id or proto.is_virtual(f.src) or f.type in LINK_LOCAL:
            return  # eigen frames, ook die van onze virtuele nodes die via een andere bridge terugkomen
        if not self._replay.accept(f.src, f.key):
            self.stats["dups"] += 1
            return
        self.stats["rx"] += 1
        self._handle(f)
        if self.relay_cb:
            self.relay_cb(data, addr)

    def _node(self, nid: int) -> NodeInfo | None:
        """De NodeInfo van een node; maakt hem aan bij het eerste frame. Infrastructuur-ids tellen niet mee."""
        if nid >= PEER_ID_MIN:
            return None
        info = self.nodes.get(nid)
        if info is None:
            info = self.nodes[nid] = NodeInfo(nid, first_seen=self._wall())
            self._emit("node", what="new", node=nid)
        return info

    def _seen(self, info: NodeInfo, f: Frame) -> None:
        info.last_seen = self._wall()
        self._dirty = True
        if not info.online:
            info.online = True
            self._emit("node", what="online", node=info.id)
        if info.boot is not None and f.boot != info.boot:
            # De node is herstart (of opnieuw geflasht): zijn versieteller kan opnieuw begonnen zijn.
            for r in info.relays:
                r.ver_known = False
        info.boot = f.boot

    def _handle(self, f: Frame) -> None:
        info = self._node(f.src)
        if info is None:
            self.stats["ignored"] += 1
            return
        self._seen(info, f)
        msg = proto.parse(f)
        if f.type == T.HEARTBEAT and msg:
            self._on_heartbeat(info, msg)
        elif f.type == T.LIGHT_STATE and msg:
            self._on_light_state(info, msg)
        elif f.type == T.BTN_EVENT and msg:
            self._on_button(info, f, msg)
        elif f.type == T.CFG_ACK and msg:
            self._on_cfg_ack(info, msg)

    def _on_heartbeat(self, info: NodeInfo, hb: proto.Heartbeat) -> None:
        changed = (info.fw, info.flags, info.nbtn, info.nind, len(info.relays)) != (hb.fw, hb.flags, hb.nbtn, hb.nind, len(hb.relays))
        info.fw, info.flags, info.nbtn, info.nind, info.cfg_hash = hb.fw, hb.flags, hb.nbtn, hb.nind, hb.cfg_hash
        while len(info.relays) < len(hb.relays):
            info.relays.append(RelayState())
        del info.relays[len(hb.relays):]
        for i, (on, ver) in enumerate(hb.relays):
            self._note_light(info, i, on, ver, None, "heartbeat")
        if changed:
            self._emit("node", what="info", node=info.id, log=False)
        self._check_cfg(info)

    def _note_light(self, info: NodeInfo, relay: int, on: bool, ver: int, cause: dict | None, via: str) -> None:
        while len(info.relays) <= relay:
            info.relays.append(RelayState())
        st = info.relays[relay]
        if st.ver_known and st.on is not None and not proto.newer16(ver, st.ver):
            return  # niet nieuwer dan wat we weten
        changed = st.on != on
        st.on, st.ver, st.ver_known = on, ver, True
        self._dirty = True
        if changed:
            self._emit("light", node=info.id, relay=relay, on=on, ver=ver, cause=cause, via=via)

    def _on_light_state(self, info: NodeInfo, ls: proto.LightState) -> None:
        cause = {"src": ls.cause_src, "btn": ls.cause_btn, "evt": ls.cause_evt} if ls.cause_src else None
        self._note_light(info, ls.relay, ls.on, ls.ver, cause, "state")
        self.virt.on_light_state(info.id, ls)
        pc = self.pending_cmds.get((info.id, ls.relay))
        if pc and ls.on == pc.target:
            del self.pending_cmds[(info.id, ls.relay)]
            self._emit("cmd", node=info.id, relay=ls.relay, target=pc.target, result="ok", ms=int((self._clock() - pc.started) * 1000))

    def _on_button(self, info: NodeInfo, f: Frame, b: proto.BtnEvent) -> None:
        if b.action != 0:
            return
        # Een herhaling door de afzender (geen antwoord gekregen) heeft een nieuw frame-id maar hetzelfde evt-nummer.
        k = (f.src, f.boot, b.btn, b.evt)
        if k in self._btn_seen:
            self.virt.on_duplicate(k)
            return
        self._btn_seen[k] = None
        while len(self._btn_seen) > 256:
            self._btn_seen.popitem(last=False)
        self._note_button(info.id, b.btn, b.evt)
        self.virt.on_mesh_button(k, f.src, b.btn, b.evt)

    def _note_button(self, node: int, btn: int, evt: int) -> None:
        """Een druk (van een echte node of een extern apparaat): logboek, MQTT en leer-detectie."""
        self._emit("btn", node=node, btn=btn, evt=evt)
        if self.learn:
            d = self.learn.detected.setdefault((node, btn), {"count": 0, "last": 0.0})
            d["count"] += 1
            d["last"] = self._wall()
            self.learn.last_activity = self._clock()
            self._emit("learn", what="detected", node=node, btn=btn, count=d["count"])

    # ------------------------------------------------------------ licht schakelen (virtuele knop)

    def command_light(self, node: int, relay: int, on: bool) -> bool:
        """Zet een licht absoluut aan of uit. Herhaald als nieuw frame tot het licht bevestigt; absolute
        opdrachten zijn idempotent, een toggle zou bij een herhaling twee keer schakelen."""
        if node in self.virt.devs:
            return self.virt.command(node, relay, on)
        info = self.nodes.get(node)
        if info is None or relay >= len(info.relays):
            return False
        self.pending_cmds[(node, relay)] = PendingCmd(on, self._clock())
        self._emit("cmd", node=node, relay=relay, target=on, result="sent", log=False)
        self._send_cmd(node, relay, on)
        self._at(self.t.cmd_retry[0], lambda: self._cmd_retry(node, relay, on, 1))
        return True

    def toggle_light(self, node: int, relay: int) -> bool:
        if node in self.virt.devs:
            return self.virt.toggle(node, relay)
        info = self.nodes.get(node)
        if info is None or relay >= len(info.relays):
            return False
        return self.command_light(node, relay, not bool(info.relays[relay].on))

    def _send_cmd(self, node: int, relay: int, on: bool) -> None:
        self.send(T.CMD_LIGHT, proto.cmd_light(node, relay, Mode.ON if on else Mode.OFF))

    def _cmd_retry(self, node: int, relay: int, on: bool, attempt: int) -> None:
        pc = self.pending_cmds.get((node, relay))
        if pc is None or pc.target != on:
            return  # bevestigd, of vervangen door een nieuwere opdracht
        if attempt >= len(self.t.cmd_retry):
            del self.pending_cmds[(node, relay)]
            self._emit("cmd", node=node, relay=relay, target=on, result="failed")
            return
        pc.tries += 1
        self._send_cmd(node, relay, on)
        self._at(self.t.cmd_retry[attempt], lambda: self._cmd_retry(node, relay, on, attempt + 1))

    def identify(self, node: int) -> None:
        self.send(T.IDENTIFY, proto.identify(node))

    # ------------------------------------------------------------ leer-modus

    def learn_start(self, target: tuple[int, int] | None = None) -> None:
        if self.learn:
            self._learn_end("replaced")
        now = self._clock()
        self.learn = Learn(target, now, now)
        self.send(T.LEARN, proto.learn(True))
        self._at(self.t.learn_refresh, self._learn_tick)
        self._emit("learn", what="started", target=list(target) if target else None)

    def _learn_tick(self) -> None:
        l = self.learn
        if not l:
            return
        if self._clock() - l.last_activity > self.t.learn_idle:
            self.learn_stop("timeout")
            return
        self.send(T.LEARN, proto.learn(True))  # nodes laten het leren na 120 s los
        self._at(self.t.learn_refresh, self._learn_tick)

    def _learn_end(self, why: str) -> None:
        """Zet het leren uit. Drie keer, want een node die het mist actueert tijdens het leren niet meer na 2 min."""
        self.learn = None
        self.send(T.LEARN, proto.learn(False))
        self._at(0.4, lambda: self.send(T.LEARN, proto.learn(False)))
        self._at(1.2, lambda: self.send(T.LEARN, proto.learn(False)))
        self._emit("learn", what="stopped", why=why)

    def learn_stop(self, why: str = "user") -> None:
        if self.learn:
            self._learn_end(why)

    def learn_save(self, sources: list[dict]) -> None:
        """Slaat de gekozen schakelaars op als triggers van het doellicht en stuurt ze naar de nodes."""
        l = self.learn
        if not l or l.target is None:
            raise ModelError("er loopt geen leer-sessie met een doellicht")
        node, relay = l.target
        self.set_triggers(node, relay, sources)
        self._emit("learn", what="saved", target=[node, relay], n=len(sources))
        self._learn_end("saved")

    # ------------------------------------------------------------ configuratie naar de nodes

    def set_triggers(self, node: int, relay: int, triggers: list[dict]) -> None:
        """Wijzigt welke knoppen een licht bedienen en brengt de betrokken nodes bij."""
        old = self.model.lights.get((node, relay))
        before = {t.node for t in old.triggers} if old else set()
        light = self.model.set_light(node, relay, triggers=triggers)
        affected = {node} | before | {t.node for t in light.triggers}
        for n in affected:
            self.model.adopt(n)
        for n in sorted(affected):
            self.sync_node(n)

    def expected(self, info: NodeInfo) -> tuple[list, list] | None:
        if info.nind is None:
            return None
        return (
            self.model.desired_relays(info.id, len(info.relays)),
            self.model.desired_follows(info.id, info.nind),
        )

    def expected_hash(self, info: NodeInfo) -> int | None:
        e = self.expected(info)
        return proto.config_hash(*e) if e else None

    def _desired_items(self, info: NodeInfo) -> dict[tuple[str, int], tuple]:
        e = self.expected(info)
        if not e:
            return {}
        relays, follows = e
        items: dict[tuple[str, int], tuple] = {("t", i): tuple(t) for i, t in enumerate(relays)}
        items.update({("f", i): (f,) for i, f in enumerate(follows)})
        return items

    def resync_all(self) -> None:
        """Na een wijziging die meerdere nodes raakt (extern apparaat toegevoegd of verwijderd)."""
        for nid in sorted(self.nodes):
            if self.model.adopted(nid):
                self.sync_node(nid)

    def sync_node(self, nid: int, full: bool = False) -> bool:
        """Stuurt wat een node mist (of alles bij full). Geeft False als er niets te doen is of de node onbekend is."""
        info = self.nodes.get(nid)
        if info is None or info.virtual:  # een extern apparaat krijgt nooit configuratie
            return False
        if info.push is not None:
            info.resync = True  # na afloop nog eens, met de nieuwste inhoud
            return False
        items = self._desired_items(info)
        if not full:
            items = {k: v for k, v in items.items() if info.sent.get(k) != v}
        if not items:
            return False
        info.push = Push(items)
        info.cfg_status = "pushing"
        self._emit("cfg", node=nid, status="pushing", n=len(items), log=False)
        self._push_send(info, info.push, list(items))
        return True

    def _push_send(self, info: NodeInfo, push: Push, items: list[tuple[str, int]]) -> None:
        push.tries += 1
        for i, item in enumerate(items):
            self._at(i * self.t.cfg_spacing, lambda it=item: self._push_item(info, push, it))
        wait = self.t.cfg_retry[min(push.tries - 1, len(self.t.cfg_retry) - 1)] + len(items) * self.t.cfg_spacing
        self._at(wait, lambda: self._push_check(info, push))

    def _push_item(self, info: NodeInfo, push: Push, item: tuple[str, int]) -> None:
        if info.push is not push or item in push.acked:
            return
        kind, idx = item
        content = push.content[item]
        if kind == "t":
            payload = proto.cfg_triggers(info.id, idx, list(content))
        else:
            f = content[0]
            payload = proto.cfg_follow(info.id, idx, None if f is None else f[0], 0 if f is None else f[1])
        self.send(T.CFG_SET, payload)

    def _push_check(self, info: NodeInfo, push: Push) -> None:
        if info.push is not push:
            return
        missing = [k for k in push.content if k not in push.acked]
        if not missing:
            return
        if push.tries > len(self.t.cfg_retry):
            info.push = None
            info.cfg_status = "failed"
            info.next_auto_push = self._clock() + self.t.cfg_grace
            self._emit("cfg", node=info.id, status="failed", missing=len(missing))
            self._resync(info)
            return
        self._push_send(info, push, missing)

    def _on_cfg_ack(self, info: NodeInfo, ack: proto.CfgAck) -> None:
        push = info.push
        if ack.dst != self.id or push is None:
            return
        item = ("t" if ack.kind == 0 else "f", ack.idx)
        if item not in push.content or item in push.acked:
            return
        push.acked.add(item)
        info.sent[item] = push.content[item]
        if len(push.acked) == len(push.content):
            info.push = None
            info.cfg_status = "applied"
            info.next_auto_push = self._clock() + self.t.cfg_grace  # eerst de volgende heartbeat afwachten
            self._emit("cfg", node=info.id, status="applied", n=len(push.content))
            self._resync(info)

    def _resync(self, info: NodeInfo) -> None:
        if info.resync:
            info.resync = False
            self.sync_node(info.id)

    def _check_cfg(self, info: NodeInfo) -> None:
        """Vergelijkt de config-hash uit de heartbeat met wat het model voor deze node voorschrijft."""
        if info.cfg_hash is None or info.nind is None:
            info.cfg_status = "unknown"
            return
        now = self._clock()
        want = self.expected_hash(info)
        if info.cfg_hash == want:
            if info.cfg_status != "ok":
                was = info.cfg_status
                info.cfg_status = "ok"
                self._emit("cfg", node=info.id, status="ok", log=was in ("drift", "failed", "applied", "pushing"))
            info.sent = self._desired_items(info)
            info.backoff = 0.0
            info.next_auto_push = 0.0  # bevestigd: een latere afwijking meteen aanpakken, niet pas na de wachttijd van de push
            return
        if not self.model.adopted(info.id):
            info.cfg_status = "unmanaged"
            return
        if info.push is not None or now < info.next_auto_push:
            return  # we zijn bezig, of net klaar en wachten op een nieuwere heartbeat
        info.cfg_status = "drift"
        info.backoff = min(max(info.backoff * 2, self.t.auto_push_base), self.t.auto_push_max)
        info.next_auto_push = now + info.backoff
        self._emit("cfg", node=info.id, status="drift")
        info.sent = {}
        self.sync_node(info.id, full=True)

    # ------------------------------------------------------------ onderhoud

    def _maintenance(self) -> None:
        now = self._wall()
        for info in self.nodes.values():
            if info.virtual:
                continue  # de online-status van een extern apparaat komt uit de HA-koppeling
            if info.online and now - info.last_seen > self.t.offline_after:
                info.online = False
                self._emit("node", what="offline", node=info.id)
        if self._dirty:
            self._save_state()
        self._at(self.t.maintenance, self._maintenance)

    def close(self) -> None:
        if self.learn:
            self._learn_end("shutdown")
        self._save_state()

    # ------------------------------------------------------------ bewaren

    def _load_state(self) -> None:
        saved_boot = None
        if self.state_path and self.state_path.exists():
            try:
                d = json.loads(self.state_path.read_text(encoding="utf-8"))
                saved_boot = int(d["boot"])
                self._vver = {str(k): int(v) for k, v in d.get("vver", {}).items()}
                for k, v in d.get("nodes", {}).items():
                    if proto.is_virtual(int(k)):
                        continue
                    n = NodeInfo(int(k), v.get("first_seen", 0.0), v.get("last_seen", 0.0), False, v.get("fw", 0), v.get("flags", 0), v.get("boot"), v.get("nbtn"), v.get("nind"), v.get("cfg_hash"))
                    n.relays = [RelayState(bool(on), ver, True) for on, ver in v.get("relays", [])]
                    n.online = self._wall() - n.last_seen < self.t.offline_after
                    self.nodes[n.id] = n
            except (OSError, ValueError, KeyError, TypeError):
                saved_boot = None
        if saved_boot is None:
            # Geen bewaarde staat: begin op een tijdsafhankelijke waarde, zodat de nodes ons niet als replay zien.
            saved_boot = int(self._wall() // 60) & 0xFFFF
        self.boot = (saved_boot + 1) & 0xFFFF
        self._save_state()

    def _vver_now(self) -> dict[str, int]:
        if not hasattr(self, "virt"):
            return dict(self._vver)
        return {f"{vid}:{i}": g.ver for vid, gangs in self.virt.devs.items() for i, g in enumerate(gangs)}

    def vver_start(self, vid: int, i: int) -> int:
        """Beginwaarde van de versieteller van een externe gang. Touches negeren een lagere teller dan ze
        kennen, dus na een herstart gaan we ruim voorbij de bewaarde waarde (tijdsafhankelijk zonder)."""
        saved = self._vver.get(f"{vid}:{i}")
        if saved is not None:
            return (saved + 17) & 0xFFFF
        return int(self._wall()) & 0x7FFF

    def _save_state(self) -> None:
        self._dirty = False
        if not self.state_path:
            return
        d = {
            "boot": self.boot,
            "nodes": {
                str(n.id): {
                    "first_seen": n.first_seen,
                    "last_seen": n.last_seen,
                    "fw": n.fw,
                    "flags": n.flags,
                    "boot": n.boot,
                    "nbtn": n.nbtn,
                    "nind": n.nind,
                    "cfg_hash": n.cfg_hash,
                    "relays": [[bool(r.on), r.ver] for r in n.relays],
                }
                for n in self.nodes.values()
                if not n.virtual
            },
            "vver": self._vver_now(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ------------------------------------------------------------ overzicht voor UI en MQTT

    def node_view(self, info: NodeInfo) -> dict:
        m = self.model.nodes.get(info.id, {})
        return {
            "id": info.id,
            "name": m.get("name", ""),
            "room": m.get("room", ""),
            "adopted": bool(m.get("adopted")),
            "online": info.online,
            "last_seen": info.last_seen,
            "fw": fw_str(info.fw) if info.fw else "",
            "bridge": info.bridge,
            "nbtn": info.nbtn,
            "nind": info.nind,
            "cfg_status": info.cfg_status,
            "virtual": info.virtual,
            "gangs": list(self.model.virtual.get(info.id, [])) if info.virtual else None,
            "relays": [{"on": r.on, "ver": r.ver} for r in info.relays],
            "follow": [
                {"ind": i, "light": f and list(f), "manual": (info.id, i) in self.model.follow}
                for i, f in enumerate(self.model.desired_follows(info.id, info.nind or 0))
            ],
        }

    def light_state(self, node: int, relay: int) -> bool | None:
        info = self.nodes.get(node)
        if info is None or relay >= len(info.relays):
            return None
        return info.relays[relay].on

    def _clock_view(self) -> dict:
        now = self.local_now()
        return {"time": now.strftime("%H:%M"), "day": now.weekday(), "tz": str(getattr(self, "tz", None) or "tijd van de add-on")}

    def snapshot(self) -> dict:
        values = self.module_values()
        lights = []
        for key, l in sorted(self.model.lights.items()):
            info = self.nodes.get(l.node)
            d = l.as_dict()
            d["on"] = self.light_state(*key)
            d["online"] = bool(info and info.online)
            d["pending"] = key in self.pending_cmds or self.virt.pending(key)
            lights.append(d)
        free = [
            {"node": n.id, "relay": i, "on": r.on}
            for n in sorted(self.nodes.values(), key=lambda n: n.id)
            for i, r in enumerate(n.relays)
            if not n.virtual and (n.id, i) not in self.model.lights
        ]
        learn = None
        if self.learn:
            l = self.learn
            learn = {
                "target": list(l.target) if l.target else None,
                "detected": [{"node": k[0], "btn": k[1], **v} for k, v in l.detected.items()],
                "idle_left": max(0.0, self.t.learn_idle - (self._clock() - l.last_activity)),
            }
        return {
            "peer_id": self.id,
            "rev": self.model.rev,
            "nodes": [self.node_view(n) for n in sorted(self.nodes.values(), key=lambda n: n.id)],
            "lights": lights,
            "free_relays": free,
            "learn": learn,
            "groups": self.groups.view(),
            "timers": [{"id": k, **t, "on": self.timer_on(k)} for k, t in sorted(self.model.timers.items(), key=lambda kv: kv[1]["name"].lower())],
            "modules": [{"id": k, **m, "out": values.get(k)} for k, m in self.model.modules.items()],
            "clock": self._clock_view(),
            "stats": dict(self.stats),
        }
