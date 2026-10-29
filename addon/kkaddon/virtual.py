"""Externe apparaten: schakelaars die geen ESP-NOW spreken (bv. Tuya via local-tuya in HA) doen mee als
"virtuele node". De add-on vertegenwoordigt ze op de mesh met een eigen node-id (0xE000..0xEFFF).

Per gang van zo'n apparaat (relais i = knop i):
  * een bevel uit de mesh (BTN_EVENT van een echte node, of een andere gang) wordt een absoluut
    `turn_on`/`turn_off` naar de HA-entiteit. Toggle rekent vanuit de laatst opgedragen stand, zodat twee
    snelle drukken niet twee keer hetzelfde bevel geven.
  * een stand-wijziging die wij niet zelf opdroegen is een druk op de fysieke touch: de add-on zendt een
    BTN_EVENT als die node uit (echte eigenaren schakelen er gewoon op, de leer-modus ziet hem) en, voor een
    gang met lamp, een LIGHT_STATE zodat de LED's van andere touches meebewegen.
  * een LIGHT_STATE met `cause = (bron, knop, evt)` na elk bevel, zodat de afzender zijn eigen event
    bevestigd ziet (de herhaal-logica van de touch hangt daaraan).
  * heartbeats als die node, zodat een herstarte touch zijn LED meteen goed heeft.

Alles loopt op de klok van de engine; HA zelf (de websocket) zit in habridge.py.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import proto
from .proto import T, is_virtual

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

CMD_RETRY_AT = 2.5  # s: HA opnieuw vragen als de stand nog niet veranderd is
CMD_FAIL_AT = 8.0  # s: opgeven
ACK_RETRY = (0.15, 0.3, 0.6, 1.2)  # s: een druk herhalen als de echte eigenaar niet bevestigt
HB_EVERY = (45.0, 75.0)
HB_SOON = (8.0, 20.0)


@dataclass
class Cmd:
    target: bool
    cause: tuple[int, int, int]  # (bron, knop, evt) zoals in LIGHT_STATE
    started: float


@dataclass
class Gang:
    entity: str
    name: str
    lamp: bool
    known: bool | None = None  # laatst bekende stand uit HA
    avail: bool = False
    ver: int = 0
    cmd: Cmd | None = None
    evt: int = 0


def parse_state(st: str | None) -> tuple[bool, bool | None]:
    """HA-toestand -> (beschikbaar, aan). 'unavailable'/'unknown'/ontbrekend: (False, None)."""
    if st is None or st in ("unavailable", "unknown"):
        return False, None
    return True, st == "on"


class Virtuals:
    def __init__(self, eng: "Engine") -> None:
        self.e = eng
        self.devs: dict[int, list[Gang]] = {}
        self.link = False
        self._by_entity: dict[str, list[tuple[int, int]]] = {}
        self._dup: OrderedDict[tuple[int, int, int, int], list[tuple[int, int]]] = OrderedDict()
        self._wait: dict[tuple[int, int, int], dict] = {}
        self._hb_started = False

    # ------------------------------------------------------------ opbouw

    def entities(self) -> set[str]:
        return set(self._by_entity)

    def pending(self, key: tuple[int, int]) -> bool:
        gangs = self.devs.get(key[0])
        return bool(gangs and key[1] < len(gangs) and gangs[key[1]].cmd)

    def rebuild(self) -> None:
        from .engine import NodeInfo  # cirkelvrij importeren

        e = self.e
        for vid in [v for v in self.devs if v not in e.model.virtual]:
            del self.devs[vid]
            e.nodes.pop(vid, None)
        self._by_entity = {}
        for vid, gangs in e.model.virtual.items():
            old = {g.entity: g for g in self.devs.get(vid, [])}
            new: list[Gang] = []
            for i, g in enumerate(gangs):
                gg = Gang(g["entity"], g["name"], g["lamp"])
                o = old.get(g["entity"])
                if o:
                    gg.known, gg.avail, gg.ver, gg.cmd, gg.evt = o.known, o.avail, o.ver, o.cmd, o.evt
                else:
                    gg.ver = e.vver_start(vid, i)
                new.append(gg)
                self._by_entity.setdefault(g["entity"], []).append((vid, i))
            self.devs[vid] = new
            info = e.nodes.get(vid)
            if info is None or not info.virtual:
                info = e.nodes[vid] = NodeInfo(vid, first_seen=e._wall(), virtual=True)
            info.nbtn, info.nind, info.cfg_status = len(new), 0, "extern"
            self._sync_info(vid)
        if self.devs and not self._hb_started:
            self._hb_started = True
            e._at(e._rng.uniform(1.0, 3.0), self._hb_loop)

    def _sync_info(self, vid: int) -> None:
        from .engine import RelayState

        e = self.e
        info, gangs = e.nodes[vid], self.devs[vid]
        info.relays = [RelayState(g.known, g.ver, True) for g in gangs]
        online = self.link and any(g.avail for g in gangs)
        if online:
            info.last_seen = e._wall()
        if online != info.online:
            info.online = online
            e._emit("node", what="online" if online else "offline", node=vid)

    def set_link(self, up: bool) -> None:
        if up == self.link:
            return
        self.link = up
        for vid in self.devs:
            self._sync_info(vid)

    # ------------------------------------------------------------ van HA

    def ha_sync(self, states: dict[str, str]) -> None:
        """Volledige momentopname (na het verbinden): stil bijwerken, een wijziging is geen druk."""
        for vid, gangs in self.devs.items():
            changed = False
            for i, g in enumerate(gangs):
                avail, on = parse_state(states.get(g.entity))
                g.avail = avail
                if on is None:
                    g.known = None
                elif on != g.known:
                    self._set_known(vid, i, on, None)
                    changed = True
            self._sync_info(vid)
            if changed or any(g.known is not None for g in gangs):
                self._hb_soon(vid, fast=True)

    def ha_event(self, entity: str, st: str | None) -> None:
        avail, on = parse_state(st)
        for vid, i in self._by_entity.get(entity, []):
            g = self.devs[vid][i]
            g.avail = avail
            if on is None:
                g.known = None
                self._sync_info(vid)
                continue
            prev = g.known
            if prev is None:  # eerste kennis (of terug na 'unavailable'): geen druk
                self._set_known(vid, i, on, None)
                self._sync_info(vid)
                self._hb_soon(vid, fast=True)
                continue
            if on == prev:
                self._sync_info(vid)
                continue
            cmd = g.cmd
            if cmd and cmd.target == on:  # het gevolg van ons eigen bevel
                g.cmd = None
                self._set_known(vid, i, on, cmd.cause)
                self.e._emit("cmd", node=vid, relay=i, target=on, result="ok", ms=int((self.e._clock() - cmd.started) * 1000))
                self._send_state(vid, i, cmd.cause)
                self._hb_soon(vid)
            else:
                self._press(vid, i, on)
            self._sync_info(vid)

    def _set_known(self, vid: int, i: int, on: bool, cause: tuple[int, int, int] | None) -> None:
        g = self.devs[vid][i]
        g.known = on
        g.ver = (g.ver + 1) & 0xFFFF
        self.e._dirty = True
        c = {"src": cause[0], "btn": cause[1], "evt": cause[2]} if cause and cause[0] else None
        self.e._emit("light", node=vid, relay=i, on=on, ver=g.ver, cause=c, via="ha")

    def _press(self, vid: int, i: int, on: bool) -> None:
        """Een niet-opgedragen wijziging: iemand drukte op de touch van het apparaat zelf."""
        g = self.devs[vid][i]
        g.evt = (g.evt + 1) & 0xFF
        cause = (vid, i, g.evt)
        self._set_known(vid, i, on, cause)
        self.e._note_button(vid, i, g.evt)
        self.e.send_as(vid, T.BTN_EVENT, proto.btn_event(i, g.evt))
        if g.lamp:
            self._send_state(vid, i, cause)
        self._track(vid, i, g.evt)
        if not self.e.learn:
            self._fire(vid, i, g.evt, vid, i)
        self._hb_soon(vid)

    # ------------------------------------------------------------ uit de mesh

    def on_mesh_button(self, k: tuple[int, int, int, int], src: int, btn: int, evt: int) -> None:
        hits = [] if self.e.learn else self._fire(src, btn, evt, src, btn)
        self._dup[k] = hits
        while len(self._dup) > 128:
            self._dup.popitem(last=False)

    def on_duplicate(self, k: tuple[int, int, int, int]) -> None:
        """De afzender herhaalt zijn druk (hij kreeg geen bevestiging): bevestig opnieuw als we al klaar zijn."""
        for n, r in self._dup.get(k, []):
            if self.devs[n][r].cmd is None:
                self._send_state(n, r, (k[0], k[2], k[3]))

    def on_light_state(self, owner: int, ls: proto.LightState) -> None:
        if not is_virtual(ls.cause_src):
            return
        key = (ls.cause_src, ls.cause_btn, ls.cause_evt)
        w = self._wait.get(key)
        if w:
            w["pairs"].discard((owner, ls.relay))
            if not w["pairs"]:
                del self._wait[key]

    def _fire(self, src: int, btn: int, evt: int, own_vid: int, own_btn: int) -> list[tuple[int, int]]:
        """Alle externe lichten waarvan (src, btn) een trigger is, behalve de gang die zelf gedrukt werd."""
        hits = []
        for (n, r), l in sorted(self.e.model.lights.items()):
            if n not in self.devs or r >= len(self.devs[n]) or (n == own_vid and r == own_btn):
                continue
            for t in l.triggers:
                if t.node == src and t.btn == btn:
                    hits.append((n, r))
                    self._act(n, r, t.mode, (src, btn, evt))
                    break
        return hits

    def _act(self, n: int, r: int, mode: str, cause: tuple[int, int, int]) -> None:
        g = self.devs[n][r]
        if mode == "toggle":
            base = g.cmd.target if g.cmd else g.known
            if base is None:
                self.e._emit("cmd", node=n, relay=r, target=None, result="unknown")
                return
            target = not base
        else:
            target = mode == "on"
        self.command(n, r, target, cause)

    # ------------------------------------------------------------ bevelen

    def command(self, vid: int, relay: int, on: bool, cause: tuple[int, int, int] | None = None) -> bool:
        gangs = self.devs.get(vid)
        if gangs is None or relay >= len(gangs):
            return False
        g = gangs[relay]
        e = self.e
        if cause is None:
            e._ui_evt = (e._ui_evt + 1) & 0xFF
            cause = (e.id, 0xFF, e._ui_evt)
        cmd = g.cmd = Cmd(on, cause, e._clock())
        e._emit("cmd", node=vid, relay=relay, target=on, result="sent", log=False)
        if not (self.link and e.ha_call and e.ha_call(g.entity, on)):
            g.cmd = None
            e._emit("cmd", node=vid, relay=relay, target=on, result="failed")
            return True
        if g.known == on:  # staat er al: alleen bevestigen
            g.cmd = None
            e._emit("cmd", node=vid, relay=relay, target=on, result="ok", ms=0)
            self._send_state(vid, relay, cause)
            return True
        e._at(CMD_RETRY_AT, lambda: self._retry(vid, relay, cmd))
        e._at(CMD_FAIL_AT, lambda: self._fail(vid, relay, cmd))
        return True

    def toggle(self, vid: int, relay: int) -> bool:
        gangs = self.devs.get(vid)
        if gangs is None or relay >= len(gangs):
            return False
        g = gangs[relay]
        base = g.cmd.target if g.cmd else g.known
        return base is not None and self.command(vid, relay, not base)

    def _retry(self, vid: int, relay: int, cmd: Cmd) -> None:
        g = self.devs.get(vid, [None] * (relay + 1))[relay]
        if g is not None and g.cmd is cmd and self.e.ha_call:
            self.e.ha_call(g.entity, cmd.target)

    def _fail(self, vid: int, relay: int, cmd: Cmd) -> None:
        g = self.devs.get(vid, [None] * (relay + 1))[relay]
        if g is not None and g.cmd is cmd:
            g.cmd = None
            self.e._emit("cmd", node=vid, relay=relay, target=cmd.target, result="failed")

    # ------------------------------------------------------------ naar de mesh

    def _send_state(self, vid: int, i: int, cause: tuple[int, int, int]) -> None:
        g = self.devs[vid][i]
        if g.lamp and g.known is not None:
            self.e.send_as(vid, T.LIGHT_STATE, proto.light_state(i, g.known, g.ver, *cause))

    def _track(self, vid: int, i: int, evt: int) -> None:
        """Wacht op de bevestiging van de echte eigenaren; herhaal de druk als nieuw frame als die uitblijft."""
        e = self.e
        pairs = set()
        for (n, r), l in e.model.lights.items():
            info = e.nodes.get(n)
            if is_virtual(n) or info is None or not info.online:
                continue
            for t in l.triggers:
                if t.node == vid and t.btn == i:
                    cur = e.light_state(n, r)
                    if t.mode == "toggle" or cur is None or cur != (t.mode == "on"):
                        pairs.add((n, r))
                    break
        if pairs:
            key = (vid, i, evt)
            self._wait[key] = {"pairs": pairs}
            e._at(ACK_RETRY[0], lambda: self._ack_retry(key, 0))

    def _ack_retry(self, key: tuple[int, int, int], n: int) -> None:
        if key not in self._wait:
            return
        if n >= len(ACK_RETRY):
            del self._wait[key]
            return
        vid, i, evt = key
        self.e.send_as(vid, T.BTN_EVENT, proto.btn_event(i, evt), copies=1)
        self.e._at(ACK_RETRY[min(n + 1, len(ACK_RETRY) - 1)], lambda: self._ack_retry(key, n + 1))

    # ------------------------------------------------------------ heartbeats

    def _hb_loop(self) -> None:
        for vid in list(self.devs):
            self._send_hb(vid)
        self.e._at(self.e._rng.uniform(*HB_EVERY), self._hb_loop)

    def _hb_soon(self, vid: int, fast: bool = False) -> None:
        self.e._at(1.0 if fast else HB_SOON[0], lambda: self._send_hb(vid))
        self.e._at(HB_SOON[1], lambda: self._send_hb(vid))

    def _send_hb(self, vid: int) -> None:
        gangs = self.devs.get(vid)
        if not gangs or any(g.known is None for g in gangs):
            return  # een onbekende stand mogen we niet als 'uit' melden
        self.e.send_as(vid, T.HEARTBEAT, proto.heartbeat(0, 0, [(g.known, g.ver) for g in gangs], len(gangs), 0, 0), copies=1)
