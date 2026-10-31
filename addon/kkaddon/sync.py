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
  * Geeft de `control`-bron (aan/uit-ingang) van een actieve kring 0 of 1, dan staat de lamp op die stand en blijft hij daar:
    wat afwijkt wordt teruggezet, ook een druk op een touch. Geeft ze onbekend, dan werkt de lamp gewoon. Control gaat voor op disable.
  * Geeft de `disable`-bron (timer of module) van een kring 1, dan bedient een touch de lamp niet. Zijn eigen relais is dan al omgezet
    (dat doet het toestel zelf), dus dat zetten we terug op de stand van de lamp. Bedienen via de UI of HA blijft kunnen.

Alles loopt op de klok van de engine; HA zelf (de websocket) zit in habridge.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .virtual import parse_state

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

PENDING_FOR = 10.0  # s: zo lang wachten we op de echo van een eigen bevel voordat we het opgeven
HOLD_EVERY = 5.0  # s: zo vaak kijken we of een lamp met een aan/uit-ingang nog goed staat
HOLD_RETRY = 60.0  # s: reageert een toestel niet, dan zo lang wachten voor we het opnieuw proberen


@dataclass
class Member:
    entity: str
    known: bool | None = None  # laatst bekende stand uit HA
    avail: bool = False
    pend: list[tuple[bool, float]] = field(default_factory=list)  # opgedragen standen met uiterste tijdstip
    lamp: bool = False  # dit relais schakelt echt de lamp (de andere zijn touch-relais)


class EntityGroups:
    def __init__(self, eng: "Engine") -> None:
        self.e = eng
        self.members: dict[str, list[Member]] = {}
        self._by_entity: dict[str, str] = {}
        self._retry: dict[str, tuple[bool, float]] = {}  # kring -> laatst opgedragen stand en wanneer opnieuw

    # ------------------------------------------------------------ opbouw

    def rebuild(self) -> None:
        old = {m.entity: m for ms in self.members.values() for m in ms}
        self.members, self._by_entity = {}, {}
        for gid, g in self.e.model.groups.items():
            ms = []
            for ent in g["entities"]:
                m = old.get(ent) or Member(ent)
                m.lamp = ent in g.get("lamp", [])
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
            want = self.held(gid)
            if want is not None:
                if on != want:
                    self._hold(gid, m, prev, want, "kept")
            elif self.blocked(gid) and not m.lamp:
                self._hold(gid, m, prev)
            else:
                self._follow(gid, m)

    def blocked(self, gid: str) -> bool:
        g = self.e.model.groups.get(gid)
        return bool(g and g.get("disable") and self.e.source_on(g["disable"]))

    def held(self, gid: str) -> bool | None:
        """De stand die de aan/uit-ingang oplegt, of None als hij dat niet doet (geen bron, onbekend, of de kring staat uit)."""
        g = self.e.model.groups.get(gid)
        return self.e.source_value(g["control"]) if g and g["enabled"] and g.get("control") else None

    def hold_tick(self) -> None:
        """Brengt lampen met een aan/uit-ingang op hun stand: als de ingang omslaat, en wat daarna nog afwijkt."""
        e = self.e
        if not (e.virt.link and e.ha_call):
            return
        now = e._clock()
        for gid, ms in self.members.items():
            want = self.held(gid)
            for m in ms:
                m.pend = [(t, d) for t, d in m.pend if d > now]
            if want is None or not any(m.avail and m.known is not None and m.known != want for m in ms):
                self._retry.pop(gid, None)
                continue
            last = self._retry.get(gid)
            if last and last[0] == want and now < last[1]:
                continue
            self._retry[gid] = (want, now + HOLD_RETRY)
            self.command(gid, want, what="held")

    def _hold(self, gid: str, src: Member, prev: bool, back: bool | None = None, what: str = "blocked") -> None:
        """Een druk terwijl de lamp geblokkeerd of vastgehouden is: alleen dit relais terug naar de stand van de lamp."""
        e = self.e
        if back is None:
            lamps = [x for x in self.members[gid] if x.lamp and x.known is not None]
            back = any(x.known for x in lamps) if lamps else prev
        if back != src.known:
            if not (e.virt.link and e.ha_call and e.ha_call(src.entity, back)):
                e._emit("sync", what="failed", group=gid, entity=src.entity, on=back, why="geen verbinding met HA")
                return
            src.pend.append((back, e._clock() + PENDING_FOR))
        e._emit("sync", what=what, group=gid, entity=src.entity, on=back)

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

    # ------------------------------------------------------------ bevelen uit de UI

    def command(self, gid: str, on: bool | None = None, entity: str | None = None, what: str = "command") -> bool:
        """Een kring of één lid bedienen. `on=None` wisselt.

        Eén lid: een gewoon bevel aan die entiteit. Staat de kring aan, dan volgt de rest, net als bij een druk op de touch.
        Hele kring: alle bereikbare leden naar dezelfde stand; staat de kring aan, dan zijn dat eigen bevelen (geen druk)."""
        e = self.e
        ms = self.members.get(gid)
        if not ms:
            return False
        if entity is not None:
            m = next((x for x in ms if x.entity == entity), None)
            if m is None:
                return False
            target = on if on is not None else (None if m.known is None else not m.known)
            targets = [(m, target)] if target is not None else []
        else:
            if on is None:
                on = not any(x.known for x in self._basis(ms))  # staat de lamp aan: uit, anders aan
            # het lamp-relais eerst: dat is waar het om gaat
            targets = sorted(((x, on) for x in ms if x.avail and x.known is not None and (x.pend[-1][0] if x.pend else x.known) != on),
                             key=lambda t: not t[0].lamp)
            if not targets and any(x.avail and x.known is not None for x in ms):
                return True  # alles staat al zo
        sent = []
        now = e._clock()
        for m, target in targets:
            if not (e.virt.link and e.ha_call and e.ha_call(m.entity, target)):
                e._emit("sync", what="failed", group=gid, entity=m.entity, on=target, why="geen verbinding met HA")
                continue
            if entity is None:  # alleen bij de hele kring: de echo's zijn onze eigen bevelen
                m.pend.append((target, now + PENDING_FOR))
            sent.append((m.entity, target))
            e._at(PENDING_FOR + 0.5, lambda m=m, t=target: self._check(gid, m, t))
        if sent:
            e._emit("sync", what=what, group=gid, entity=entity, on=sent[0][1], to=[x for x, _ in sent])
        return bool(sent)

    @staticmethod
    def _basis(ms: list[Member]) -> list[Member]:
        """Welke leden bepalen de stand van de kring: de lamp-relais als die bereikbaar zijn, anders iedereen."""
        lamps = [x for x in ms if x.lamp and x.known is not None]
        return lamps or ms

    # ------------------------------------------------------------ voor de UI

    def view(self) -> list[dict]:
        out = []
        for gid, g in sorted(self.e.model.groups.items(), key=lambda kv: kv[1]["name"].lower()):
            ms = self.members.get(gid, [])
            out.append({
                "id": gid, "name": g["name"], "enabled": g["enabled"],
                "members": [{"entity": m.entity, "on": m.known, "avail": m.avail, "lamp": m.lamp} for m in ms],
                "lamp": [m.entity for m in ms if m.lamp],
                "disable": g.get("disable"), "blocked": self.blocked(gid), "control": g.get("control"), "held": self.held(gid),
            })
        return out
