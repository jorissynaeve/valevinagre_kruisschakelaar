"""Entiteitskringen: de kruisschakeling zoals AppDaemon (`kruisschakelaar`) hem deed.

Een kring is een lijst HA-entiteiten (switch/light) die elkaar volgen: verandert er één doordat iemand op een
touch drukt, dan krijgen de andere dezelfde stand (absoluut, dus ook als er eentje uit de pas liep).

  * Alleen een kring die **aan staat** doet iets. Een kring die uit staat wordt wel gevolgd (de stand verschijnt in
    de UI), zodat je hem kunt vergelijken met wat AppDaemon doet. Zet dezelfde kring in AppDaemon uit, anders
    werken ze elkaar tegen.
  * De beginstand, de terugkeer uit 'unavailable' en het opnieuw verbinden met HA zijn nooit een aanleiding om
    iets te schakelen. Er wordt ook niets rechtgezet: alleen een stand-wijziging die wij niet zelf opdroegen telt.
  * Eigen bevelen herkennen we aan de verwachte stand ('echo'). Een rij verwachtingen per entiteit, zodat twee snelle
    drukken achter elkaar geen heen-en-weer geven: we geven alleen een bevel als de laatst opgedragen (of bekende)
    stand afwijkt.
  * Een entiteit die niet beschikbaar is wordt overgeslagen; een bevel dat niet wordt opgevolgd wordt gemeld.

Alles loopt op de klok van de engine; HA zelf (de websocket) zit in habridge.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .virtual import parse_state

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

PENDING_FOR = 10.0  # s: zo lang wachten we op de echo van een eigen bevel voordat we het opgeven


@dataclass
class Member:
    entity: str
    known: bool | None = None  # laatst bekende stand uit HA
    avail: bool = False
    pend: list[tuple[bool, float]] = field(default_factory=list)  # opgedragen standen met uiterste tijdstip


class EntityGroups:
    def __init__(self, eng: "Engine") -> None:
        self.e = eng
        self.members: dict[str, list[Member]] = {}
        self._by_entity: dict[str, str] = {}

    # ------------------------------------------------------------ opbouw

    def rebuild(self) -> None:
        old = {m.entity: m for ms in self.members.values() for m in ms}
        self.members, self._by_entity = {}, {}
        for gid, g in self.e.model.groups.items():
            ms = []
            for ent in g["entities"]:
                m = old.get(ent) or Member(ent)
                ms.append(m)
                self._by_entity[ent] = gid
            self.members[gid] = ms

    def entities(self) -> set[str]:
        """Alles wat we in HA volgen: ook van kringen die uit staan, zodat de UI hun stand toont."""
        return set(self._by_entity)

    def enabled(self, gid: str) -> bool:
        g = self.e.model.groups.get(gid)
        return bool(g and g["enabled"])

    # ------------------------------------------------------------ van HA

    def ha_sync(self, states: dict[str, str]) -> None:
        """Volledige momentopname (na het verbinden of een modelwijziging): stil bijwerken."""
        changed = False
        for ms in self.members.values():
            for m in ms:
                before = (m.avail, m.known)
                m.avail, m.known = parse_state(states.get(m.entity))
                changed |= before != (m.avail, m.known)
        if changed:
            self.e._emit("grp", log=False)

    def ha_event(self, entity: str, st: str | None) -> None:
        gid = self._by_entity.get(entity)
        if gid is None:
            return
        m = next(x for x in self.members[gid] if x.entity == entity)
        now = self.e._clock()
        m.pend = [(t, d) for t, d in m.pend if d > now]
        before = (m.avail, m.known)
        m.avail, on = parse_state(st)
        if on is None:  # weg of onbekend: later terug is geen druk
            m.known = None
            m.pend.clear()
            if before != (m.avail, None):
                self.e._emit("grp", log=False, group=gid)
            return
        prev, m.known = m.known, on
        if before != (m.avail, on):
            self.e._emit("grp", log=False, group=gid)  # de pagina ververst zichzelf
        if prev is None or prev == on:
            return
        for i, (target, _) in enumerate(m.pend):
            if target == on:  # de echo van ons eigen bevel
                del m.pend[: i + 1]
                return
        if self.enabled(gid):
            self._follow(gid, m)

    def _follow(self, gid: str, src: Member) -> None:
        e = self.e
        on = bool(src.known)
        now = e._clock()
        sent, skipped = [], []
        for o in self.members[gid]:
            if o is src:
                continue
            eff = o.pend[-1][0] if o.pend else o.known
            if not o.avail or o.known is None:
                skipped.append(o.entity)
                continue
            if eff == on:
                continue
            if not (e.virt.link and e.ha_call and e.ha_call(o.entity, on)):
                e._emit("sync", what="failed", group=gid, entity=o.entity, on=on, why="geen verbinding met HA")
                continue
            o.pend.append((on, now + PENDING_FOR))
            sent.append(o.entity)
            e._at(PENDING_FOR + 0.5, lambda o=o, on=on: self._check(gid, o, on))
        e._emit("sync", what="follow", group=gid, entity=src.entity, on=on, to=sent, skipped=skipped)

    def _check(self, gid: str, m: Member, target: bool) -> None:
        if m.known != target and gid in self.members and m in self.members[gid]:
            self.e._emit("sync", what="failed", group=gid, entity=m.entity, on=target, why="geen reactie van het apparaat")

    # ------------------------------------------------------------ voor de UI

    def view(self) -> list[dict]:
        out = []
        for gid, g in sorted(self.e.model.groups.items(), key=lambda kv: kv[1]["name"].lower()):
            ms = self.members.get(gid, [])
            out.append({
                "id": gid, "name": g["name"], "enabled": g["enabled"],
                "members": [{"entity": m.entity, "on": m.known, "avail": m.avail} for m in ms],
            })
        return out
