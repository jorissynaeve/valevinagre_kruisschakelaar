"""Het netwerkmodel: welke knop welk licht bedient, plus namen. Dit is de enige bron van waarheid;
de nodes krijgen hun deel via CFG_SET en de heartbeat-hash laat zien of ze het nog hebben.

Bestandsformaat (network.json, ook voor export/import):
  {"version": 1,
   "nodes":  {"2": {"name": "Relaisbox", "room": "Living", "adopted": true}},
   "lights": [{"node": 2, "relay": 2, "name": "Plafond", "enabled": true,
               "triggers": [{"node": 1, "btn": 1, "mode": "toggle"}]}],
   "follow": [{"node": 1, "ind": 1, "light": null}],      # handmatige indicator-koppelingen (null = geen)
   "groups": {"keuken": {"name": "Keuken", "enabled": false,
                         "entities": ["switch.r4b01_sonoff_4ch_r4_01_relay_4", "light.t4b001_t4b001_relay_1"],
                         "lamp": ["switch.r4b01_sonoff_4ch_r4_01_relay_4"],     # lamp: de relais die echt de lamp schakelen
                         "disable": "nacht"}},                                  # optioneel: timer die de touches blokkeert
   "timers": {"nacht": {"name": "Nacht", "days": {"mon": [{"from": "03:00", "to": "07:00"}]}}},  # per dag tot 3 intervallen; "to" 00:00 = middernacht
   "plan":   {"floors": [{"id": "g", "name": "Begane grond", "w": 1000, "h": 640,
                          "rooms": [{"x": 20, "y": 20, "w": 300, "h": 200, "name": "Living"}],
                          "items": {"n5": [120, 80], "l2.1": [200, 90]}}]},   # plattegrond, alleen weergave
   "schema": {"pages": [{"id": "keuken", "name": "Keuken"}],                    # schema: één pagina per ruimte, alleen weergave
              "blocks": {"g:keuken": {"page": "keuken", "x": 420, "y": 40}},    # g:<kring>, l<node>.<relais>, n<node>, d:<schakelaar>
              "switches": {"abc": {"name": "T4B001", "gangs": [{"entity": "switch.a", "name": "relay_1"}]}},
              "names": {"d:abc": "Touch keuken"}}}                              # eigen naam van een schakelaar-blok, naast de technische
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .proto import MAX_GANGS, MAX_IND, MAX_RELAYS, MAX_TRIG, MODE_BY_NAME, VIRTUAL_MAX, VIRTUAL_MIN, is_virtual

MAX_NAME = 60
MAX_FLOORS, MAX_ROOMS, MAX_ITEMS, PLAN_MAX = 8, 80, 400, 4000
MAX_GROUPS, MAX_GROUP_ENTITIES = 120, 40
MAX_PAGES, MAX_BLOCKS, MAX_SWITCHES, MAX_SWITCH_GANGS, SCHEMA_MAX = 60, 1000, 300, 64, 20000
MAX_TIMERS, MAX_INTERVALS = 60, 3
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FORMAT_VERSION = 1


class ModelError(ValueError):
    pass


@dataclass(frozen=True)
class Trig:
    node: int
    btn: int
    mode: str = "toggle"

    def tuple(self) -> tuple[int, int, int]:
        return (self.node, self.btn, int(MODE_BY_NAME[self.mode]))

    def as_dict(self) -> dict:
        return {"node": self.node, "btn": self.btn, "mode": self.mode}


@dataclass
class Light:
    node: int
    relay: int
    name: str = ""
    enabled: bool = True  # zichtbaar in Home Assistant
    triggers: list[Trig] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return (self.node, self.relay)

    def as_dict(self) -> dict:
        return {
            "node": self.node,
            "relay": self.relay,
            "name": self.name,
            "enabled": self.enabled,
            "triggers": [t.as_dict() for t in self.triggers],
        }


ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
FLOOR_ID_RE = re.compile(r"^[a-z0-9]{1,12}$")
GROUP_ID_RE = re.compile(r"^[a-z0-9_]{1,80}$")


def _group(gid, v) -> dict:
    if not isinstance(gid, str) or not GROUP_ID_RE.match(gid):
        raise ModelError(f"kring: ongeldig id {gid!r}")
    if not isinstance(v, dict):
        raise ModelError(f"kring {gid}: object verwacht")
    raw = v.get("entities")
    if not isinstance(raw, list) or len(raw) > MAX_GROUP_ENTITIES:  # een lamp-blok begint leeg en krijgt zijn relais één voor één
        raise ModelError(f"kring {gid}: maximaal {MAX_GROUP_ENTITIES} entiteiten")
    ents = []
    for e in raw:
        if not isinstance(e, str) or not ENTITY_RE.match(e):
            raise ModelError(f"kring {gid}: ongeldige entiteit {e!r}")
        if e in ents:
            raise ModelError(f"kring {gid}: {e} komt twee keer voor")
        ents.append(e)
    lamp = v.get("lamp") or []
    if not isinstance(lamp, list) or any(not isinstance(x, str) or x not in ents for x in lamp):
        raise ModelError(f"kring {gid}: lamp-relais moeten entiteiten van de kring zijn")
    out = {"name": _name(v.get("name"), "kring.name") or gid, "enabled": bool(v.get("enabled", False)), "entities": ents,
           "lamp": [e for e in ents if e in lamp]}
    dis = v.get("disable")
    if dis is not None:
        if not isinstance(dis, str) or not GROUP_ID_RE.match(dis):
            raise ModelError(f"kring {gid}: ongeldige timer {dis!r}")
        out["disable"] = dis
    return out


def _groups(raw) -> dict[str, dict]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_GROUPS:
        raise ModelError(f"kringen: maximaal {MAX_GROUPS}")
    return {gid: _group(gid, v) for gid, v in raw.items()}


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _minutes(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:])


def _timer(tid, v) -> dict:
    """Per dag tot 3 intervallen waarin de uitgang 1 is. Een interval loopt niet over middernacht: maak er twee van."""
    if not isinstance(tid, str) or not GROUP_ID_RE.match(tid) or not isinstance(v, dict):
        raise ModelError(f"timer: ongeldig id {tid!r}")
    name = _name(v.get("name"), "timer.name")
    if not name:
        raise ModelError("geef de timer een naam")
    raw = v.get("days") or {}
    if not isinstance(raw, dict) or set(raw) - set(DAYS):
        raise ModelError("timer: onbekende dag")
    days = {}
    for d in DAYS:
        items = raw.get(d) or []
        if not isinstance(items, list) or len(items) > MAX_INTERVALS:
            raise ModelError(f"timer: maximaal {MAX_INTERVALS} intervallen per dag")
        out = []
        for it in items:
            a, b = (it.get("from"), it.get("to")) if isinstance(it, dict) else (None, None)
            if not (isinstance(a, str) and isinstance(b, str) and TIME_RE.match(a) and TIME_RE.match(b)):
                raise ModelError("timer: tijd verwacht als UU:MM")
            if b != "00:00" and _minutes(a) >= _minutes(b):
                raise ModelError(f"timer: {a} tot {b} loopt over middernacht of is leeg; maak er twee intervallen van")
            out.append({"from": a, "to": b})
        if out:
            days[d] = sorted(out, key=lambda x: x["from"])
    return {"name": name, "days": days}


def timer_active(t: dict, weekday: int, minutes: int) -> bool:
    """Staat de uitgang op 1? Begin hoort erbij, einde niet; `to` 00:00 is middernacht aan het eind van de dag."""
    return any(_minutes(i["from"]) <= minutes and (i["to"] == "00:00" or minutes < _minutes(i["to"]))
               for i in t["days"].get(DAYS[weekday], []))


def _timers(raw) -> dict[str, dict]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_TIMERS:
        raise ModelError(f"timers: maximaal {MAX_TIMERS}")
    return {k: _timer(k, v) for k, v in raw.items()}


def _conflicts(groups: dict[str, dict], virtual: dict[int, list[dict]]) -> None:
    """Een entiteit mag maar door één ding worden bestuurd, anders werken die elkaar tegen."""
    owner: dict[str, str] = {}
    for vid, gangs in virtual.items():
        for g in gangs:
            owner.setdefault(g["entity"], f"extern apparaat {vid}")
    for gid, g in groups.items():
        if not g["enabled"]:
            continue
        for e in g["entities"]:
            if e in owner:
                raise ModelError(f"{e} wordt al bestuurd door {owner[e]} en staat ook in kring {g['name']} (aan): zet er een van beide uit of haal de entiteit eruit")
            owner[e] = f"kring {g['name']}"
ITEM_RE = re.compile(r"^(n\d{1,5}|l\d{1,5}\.\d{1,2})$")  # n<node> = schakelaar, l<node>.<relais> = lamp
PAGE_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
SWITCH_ID_RE = re.compile(r"^[a-z0-9_]{1,80}$")
BLOCK_RE = re.compile(r"^(g:[a-z0-9_]{1,80}|d:[a-z0-9_]{1,80}|t:[a-z0-9_]{1,80}|n\d{1,5}|l\d{1,5}\.\d{1,2})$")


def _switch_gangs(raw) -> list[dict]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SWITCH_GANGS:
        raise ModelError(f"een schakelaar heeft 1 tot {MAX_SWITCH_GANGS} gangen")
    out, seen = [], set()
    for g in raw:
        ent = g.get("entity") if isinstance(g, dict) else None
        if not isinstance(ent, str) or not ENTITY_RE.match(ent):
            raise ModelError(f"schakelaar: ongeldige entiteit {ent!r}")
        if ent in seen:
            raise ModelError(f"schakelaar: {ent} komt twee keer voor")
        seen.add(ent)
        out.append({"entity": ent, "name": _name(g.get("name"), "gang.name")})
    return out


def _schema(raw) -> dict:
    """Het schema: pagina's per ruimte, de plek van elk blok, en de schakelaar-blokken die geen node zijn.
    De draden staan hier niet in: die volgen uit de kringen en de triggers."""
    if raw in (None, {}):
        return {"pages": [], "blocks": {}, "switches": {}, "names": {}}
    if not isinstance(raw, dict):
        raise ModelError("schema: object verwacht")
    pages_in, blocks_in, sw_in = raw.get("pages") or [], raw.get("blocks") or {}, raw.get("switches") or {}
    if not isinstance(pages_in, list) or len(pages_in) > MAX_PAGES:
        raise ModelError(f"schema: maximaal {MAX_PAGES} ruimtes")
    if not isinstance(blocks_in, dict) or len(blocks_in) > MAX_BLOCKS or not isinstance(sw_in, dict) or len(sw_in) > MAX_SWITCHES:
        raise ModelError("schema: te veel blokken")
    pages, ids = [], set()
    for p in pages_in:
        pid = p.get("id") if isinstance(p, dict) else None
        if not isinstance(pid, str) or not PAGE_ID_RE.match(pid) or pid in ids:
            raise ModelError("schema: ruimte heeft een ontbrekend, ongeldig of dubbel id")
        ids.add(pid)
        pages.append({"id": pid, "name": _name(p.get("name"), "ruimte.name") or pid})
    blocks = {}
    for k, b in blocks_in.items():
        if not BLOCK_RE.match(str(k)) or not isinstance(b, dict):
            raise ModelError(f"schema: onbekend blok {k!r}")
        if b.get("page") not in ids:
            raise ModelError(f"schema: blok {k} staat op een onbekende ruimte")
        blocks[k] = {"page": b["page"], "x": _int(b.get("x"), 0, SCHEMA_MAX, "blok.x"), "y": _int(b.get("y"), 0, SCHEMA_MAX, "blok.y")}
    switches = {}
    for sid, s in sw_in.items():
        if not SWITCH_ID_RE.match(str(sid)) or not isinstance(s, dict):
            raise ModelError(f"schema: ongeldige schakelaar {sid!r}")
        switches[sid] = {"name": _name(s.get("name"), "schakelaar.name") or sid, "gangs": _switch_gangs(s.get("gangs"))}
    names_in = raw.get("names") or {}
    if not isinstance(names_in, dict) or len(names_in) > MAX_BLOCKS:
        raise ModelError("schema: te veel namen")
    names = {}
    for k, n in names_in.items():
        if not BLOCK_RE.match(str(k)):
            raise ModelError(f"schema: onbekend blok {k!r}")
        if _name(n, "blok.name"):
            names[k] = _name(n, "blok.name")
    return {"pages": pages, "blocks": dict(sorted(blocks.items())), "switches": switches, "names": dict(sorted(names.items()))}


def _plan(raw) -> dict:
    """Plattegrond: verdiepingen met kamers (rechthoeken) en de plek van schakelaars (`n5`) en lampen (`l2.1`).
    Het is alleen weergave: het verandert nooit iets aan de schakelingen."""
    if raw in (None, {}):
        return {"floors": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("floors"), list) or len(raw["floors"]) > MAX_FLOORS:
        raise ModelError(f"plattegrond: maximaal {MAX_FLOORS} verdiepingen")
    floors, ids, placed = [], set(), set()
    for f in raw["floors"]:
        if not isinstance(f, dict):
            raise ModelError("plattegrond: verdieping moet een object zijn")
        fid = f.get("id")
        if not isinstance(fid, str) or not FLOOR_ID_RE.match(fid) or fid in ids:
            raise ModelError("plattegrond: verdieping heeft een ontbrekend, ongeldig of dubbel id")
        ids.add(fid)
        rooms = f.get("rooms") or []
        items = f.get("items") or {}
        if not isinstance(rooms, list) or len(rooms) > MAX_ROOMS or not isinstance(items, dict) or len(items) > MAX_ITEMS:
            raise ModelError(f"plattegrond: maximaal {MAX_ROOMS} kamers en {MAX_ITEMS} items per verdieping")
        out_rooms = []
        for r in rooms:
            if not isinstance(r, dict):
                raise ModelError("plattegrond: kamer moet een object zijn")
            out_rooms.append({
                "x": _int(r.get("x"), 0, PLAN_MAX, "kamer.x"), "y": _int(r.get("y"), 0, PLAN_MAX, "kamer.y"),
                "w": _int(r.get("w"), 20, PLAN_MAX, "kamer.w"), "h": _int(r.get("h"), 20, PLAN_MAX, "kamer.h"),
                "name": _name(r.get("name"), "kamer.name"),
            })
        out_items = {}
        for k, v in items.items():
            if not ITEM_RE.match(str(k)):
                raise ModelError(f"plattegrond: onbekend item {k!r}")
            if k in placed:
                raise ModelError(f"plattegrond: {k} staat op twee verdiepingen")
            if not isinstance(v, list) or len(v) != 2:
                raise ModelError(f"plattegrond: positie van {k} moet [x, y] zijn")
            placed.add(k)
            out_items[k] = [_int(v[0], 0, PLAN_MAX, "item.x"), _int(v[1], 0, PLAN_MAX, "item.y")]
        floors.append({
            "id": fid, "name": _name(f.get("name"), "verdieping.name"),
            "w": _int(f.get("w", 1000), 200, PLAN_MAX, "verdieping.w"), "h": _int(f.get("h", 640), 200, PLAN_MAX, "verdieping.h"),
            "rooms": out_rooms, "items": dict(sorted(out_items.items())),
        })
    return {"floors": floors}


def _gangs(raw) -> list[dict]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_GANGS:
        raise ModelError(f"een extern apparaat heeft 1 tot {MAX_GANGS} gangen")
    out, seen = [], set()
    for i, g in enumerate(raw):
        if not isinstance(g, dict):
            raise ModelError("gang moet een object zijn")
        ent = g.get("entity")
        if not isinstance(ent, str) or not ENTITY_RE.match(ent):
            raise ModelError(f"gang {i + 1}: ongeldige entiteit {ent!r} (verwacht bv. switch.gang_1)")
        if ent in seen:
            raise ModelError(f"gang {i + 1}: {ent} komt twee keer voor")
        seen.add(ent)
        out.append({"entity": ent, "name": _name(g.get("name"), "gang.name"), "lamp": bool(g.get("lamp", True))})
    return out


def _int(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ModelError(f"{what}: verwacht een getal tussen {lo} en {hi}, kreeg {v!r}")
    return v


def _name(v, what):
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > MAX_NAME:
        raise ModelError(f"{what}: tekst van maximaal {MAX_NAME} tekens verwacht")
    return v.strip()


def _trigs(raw) -> list[Trig]:
    if not isinstance(raw, list):
        raise ModelError("triggers moet een lijst zijn")
    seen = {}
    for t in raw:
        if not isinstance(t, dict):
            raise ModelError("trigger moet een object zijn")
        node = _int(t.get("node"), 1, 0xEFFF, "trigger.node")
        btn = _int(t.get("btn"), 0, 7, "trigger.btn")
        mode = t.get("mode", "toggle")
        if mode not in MODE_BY_NAME:
            raise ModelError(f"trigger.mode: onbekende modus {mode!r}")
        seen[(node, btn)] = Trig(node, btn, mode)  # dezelfde knop twee keer: de laatste wint
    out = sorted(seen.values(), key=lambda t: (t.node, t.btn))
    if len(out) > MAX_TRIG:
        raise ModelError(f"een licht kan maximaal {MAX_TRIG} knoppen hebben")
    return out


class Model:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else None
        self.nodes: dict[int, dict] = {}
        self.lights: dict[tuple[int, int], Light] = {}
        self.follow: dict[tuple[int, int], tuple[int, int] | None] = {}
        self.virtual: dict[int, list[dict]] = {}  # virtuele node -> gangen [{entity, name, lamp}]
        self.plan: dict = {"floors": []}  # plattegrond (alleen weergave)
        self.groups: dict[str, dict] = {}  # entiteitskringen (AppDaemon-stijl): id -> {name, enabled, entities}
        self.schema: dict = _schema(None)  # blokkenschema per ruimte (alleen weergave)
        self.timers: dict[str, dict] = {}  # timer-modules: id -> {name, days}
        self.rev = 0
        self._listeners: list[Callable[[], None]] = []
        if self.path and self.path.exists():
            self.load_dict(json.loads(self.path.read_text(encoding="utf-8")), save=False)

    # ------------------------------------------------------------ opslag

    def on_change(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def _changed(self) -> None:
        self.rev += 1
        self.save()
        for cb in self._listeners:
            cb()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".network-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def to_dict(self) -> dict:
        d = {
            "version": FORMAT_VERSION,
            "nodes": {str(k): v for k, v in sorted(self.nodes.items())},
            "virtual": {str(k): {"gangs": g} for k, g in sorted(self.virtual.items())},
            "lights": [l.as_dict() for _, l in sorted(self.lights.items())],
            "follow": [
                {"node": n, "ind": i, "light": None if v is None else {"node": v[0], "relay": v[1]}}
                for (n, i), v in sorted(self.follow.items())
            ],
        }
        if self.plan["floors"]:
            d["plan"] = self.plan
        if self.groups:
            d["groups"] = self.groups
        if self.timers:
            d["timers"] = self.timers
        if self.schema["pages"] or self.schema["switches"] or self.schema["names"]:
            d["schema"] = self.schema
        return d

    def load_dict(self, d: dict, save: bool = True) -> None:
        """Vervangt het hele model (import). Bij een fout blijft het oude model staan."""
        if not isinstance(d, dict) or d.get("version") != FORMAT_VERSION:
            raise ModelError(f"onbekend bestandsformaat (versie {d.get('version') if isinstance(d, dict) else '?'})")
        nodes: dict[int, dict] = {}
        for k, v in (d.get("nodes") or {}).items():
            nid = _int(int(k) if str(k).isdigit() else None, 1, 0xEFFF, "node")
            nodes[nid] = {
                "name": _name(v.get("name"), "node.name"),
                "room": _name(v.get("room"), "node.room"),
                "adopted": bool(v.get("adopted", False)),
            }
        lights: dict[tuple[int, int], Light] = {}
        for l in d.get("lights") or []:
            node = _int(l.get("node"), 1, 0xEFFF, "light.node")
            relay = _int(l.get("relay"), 0, MAX_RELAYS - 1, "light.relay")
            lights[(node, relay)] = Light(
                node, relay, _name(l.get("name"), "light.name"), bool(l.get("enabled", True)), _trigs(l.get("triggers") or [])
            )
        follow: dict[tuple[int, int], tuple[int, int] | None] = {}
        for f in d.get("follow") or []:
            n = _int(f.get("node"), 1, 0xEFFF, "follow.node")
            i = _int(f.get("ind"), 0, MAX_IND - 1, "follow.ind")
            lt = f.get("light")
            if lt is None:
                follow[(n, i)] = None
            else:
                follow[(n, i)] = (_int(lt.get("node"), 1, 0xEFFF, "follow.light.node"), _int(lt.get("relay"), 0, MAX_RELAYS - 1, "follow.light.relay"))
        virtual: dict[int, list[dict]] = {}
        for k, v in (d.get("virtual") or {}).items():
            vid = _int(int(k) if str(k).isdigit() else None, VIRTUAL_MIN, VIRTUAL_MAX, "virtual id")
            virtual[vid] = _gangs((v or {}).get("gangs"))
        plan = _plan(d.get("plan"))
        groups = _groups(d.get("groups"))
        schema = _schema(d.get("schema"))
        timers = _timers(d.get("timers"))
        if save:  # bij het opstarten nooit weigeren, bij een import wel
            _conflicts(groups, virtual)
        self.nodes, self.lights, self.follow, self.virtual, self.plan, self.groups, self.schema, self.timers = nodes, lights, follow, virtual, plan, groups, schema, timers
        if save:
            self._changed()

    def set_groups(self, items) -> list[str]:
        """Import van kringen (nieuw of bijgewerkt). Een nieuwe kring staat **uit**; een bestaande houdt haar aan/uit."""
        if not isinstance(items, list) or not items:
            raise ModelError("geen kringen om te importeren")
        new = dict(self.groups)
        for it in items:
            if not isinstance(it, dict):
                raise ModelError("kring moet een object zijn")
            gid = it.get("id")
            old = new.get(gid)
            ents = it.get("entities")
            if not isinstance(ents, list) or len(ents) < 2:
                raise ModelError(f"kring {gid}: minstens 2 entiteiten verwacht")
            keep = [x for x in (old["lamp"] if old else []) if isinstance(ents, list) and x in ents]  # lamp-relais blijven bij opnieuw importeren
            new[gid] = _group(gid, {"name": it.get("name"), "entities": ents, "enabled": old["enabled"] if old else False, "lamp": keep,
                                    "disable": old.get("disable") if old else None})
        if len(new) > MAX_GROUPS:
            raise ModelError(f"kringen: maximaal {MAX_GROUPS}")
        _conflicts(new, self.virtual)
        self.groups = new
        self._changed()
        return [it["id"] for it in items]

    def add_group(self, name: str) -> str:
        """Een lege kring voor een nieuw lamp-blok. Ze staat uit, zoals elke nieuwe kring."""
        name = _name(name, "name")
        if not name:
            raise ModelError("geef de lamp een naam")
        if len(self.groups) >= MAX_GROUPS:
            raise ModelError(f"kringen: maximaal {MAX_GROUPS}")
        base = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:70] or "lamp"
        gid, n = base, 2
        while gid in self.groups:
            gid, n = f"{base}_{n}", n + 1
        self.groups = {**self.groups, gid: _group(gid, {"name": name, "entities": []})}
        self._changed()
        return gid

    def set_group(self, gid: str, name=None, entities=None, enabled=None, lamp=None, disable=...) -> None:
        old = self.groups.get(gid)
        if old is None:
            raise ModelError("onbekende kring")
        ents = old["entities"] if entities is None else entities
        if lamp is None:  # nieuwe entiteitenlijst: lamp-relais die er niet meer in staan vallen weg
            lamp = [x for x in old["lamp"] if isinstance(ents, list) and x in ents]
        if disable is ...:
            disable = old.get("disable")
        elif disable is not None and disable not in self.timers:
            raise ModelError("onbekende timer")
        new = _group(gid, {
            "disable": disable,
            "name": old["name"] if name is None else name,
            "entities": ents,
            "enabled": old["enabled"] if enabled is None else enabled,
            "lamp": lamp,
        })
        groups = {**self.groups, gid: new}
        _conflicts(groups, self.virtual)
        self.groups = groups
        self._changed()

    def save_timer(self, tid: str | None, name, days) -> str:
        if tid is None:
            if len(self.timers) >= MAX_TIMERS:
                raise ModelError(f"timers: maximaal {MAX_TIMERS}")
            base = re.sub(r"[^a-z0-9_]+", "_", str(name or "").lower()).strip("_")[:70] or "timer"
            tid, n = base, 2
            while tid in self.timers:
                tid, n = f"{base}_{n}", n + 1
        elif tid not in self.timers:
            raise ModelError("onbekende timer")
        self.timers = {**self.timers, tid: _timer(tid, {"name": name, "days": days})}
        self._changed()
        return tid

    def remove_timer(self, tid: str) -> bool:
        """Lampen die hem gebruikten zijn daarna gewoon weer te bedienen."""
        if tid not in self.timers:
            return False
        self.timers = {k: v for k, v in self.timers.items() if k != tid}
        self.groups = {k: {x: y for x, y in g.items() if not (x == "disable" and y == tid)} for k, g in self.groups.items()}
        self._changed()
        return True

    def remove_group(self, gid: str) -> bool:
        if gid not in self.groups:
            return False
        self.groups = {k: v for k, v in self.groups.items() if k != gid}
        self._changed()
        return True

    def disable_all_groups(self) -> int:
        n = sum(1 for g in self.groups.values() if g["enabled"])
        if n:
            self.groups = {k: {**v, "enabled": False} for k, v in self.groups.items()}
            self._changed()
        return n

    def set_plan(self, raw) -> None:
        """Alleen bewaren: een plattegrond verandert niets aan de schakelingen, dus geen herconfiguratie en geen `rev`."""
        self.plan = _plan(raw)
        self.save()

    def set_schema(self, raw) -> None:
        """Net als de plattegrond: alleen bewaren. Een blok verplaatsen raakt de schakelingen niet."""
        self.schema = _schema(raw)
        self.save()

    # ------------------------------------------------------------ wijzigen

    def set_node(self, node: int, name: str | None = None, room: str | None = None, adopted: bool | None = None) -> None:
        _int(node, 1, 0xEFFF, "node")
        n = self.nodes.setdefault(node, {"name": "", "room": "", "adopted": False})
        if name is not None:
            n["name"] = _name(name, "name")
        if room is not None:
            n["room"] = _name(room, "room")
        if adopted is not None:
            n["adopted"] = bool(adopted)
        self._changed()

    def adopt(self, node: int) -> None:
        n = self.nodes.get(node)
        if n and n.get("adopted"):
            return
        self.set_node(node, adopted=True)

    def adopted(self, node: int) -> bool:
        return bool(self.nodes.get(node, {}).get("adopted"))

    def node_name(self, node: int) -> str:
        return self.nodes.get(node, {}).get("name") or f"Node {node}"

    def set_light(self, node: int, relay: int, name: str | None = None, enabled: bool | None = None, triggers: list[dict] | None = None) -> Light:
        _int(node, 1, 0xEFFF, "node")
        _int(relay, 0, MAX_RELAYS - 1, "relay")
        l = self.lights.get((node, relay)) or Light(node, relay)
        if name is not None:
            l.name = _name(name, "name")
        if enabled is not None:
            l.enabled = bool(enabled)
        if triggers is not None:
            l.triggers = _trigs(triggers)
        if is_virtual(node):  # de eigen knop van een gang heeft het relais al omgezet: nooit zichzelf bedienen
            l.triggers = [t for t in l.triggers if not (t.node == node and t.btn == relay)]
        self.lights[(node, relay)] = l
        self.adopt_quiet(node)
        self._changed()
        return l

    def adopt_quiet(self, node: int) -> None:
        n = self.nodes.setdefault(node, {"name": "", "room": "", "adopted": False})
        n["adopted"] = True

    def remove_light(self, node: int, relay: int) -> bool:
        if (node, relay) not in self.lights:
            return False
        del self.lights[(node, relay)]
        self._changed()
        return True

    # ------------------------------------------------------------ externe apparaten

    def set_virtual(self, vid: int | None, name: str, room: str, gangs: list[dict]) -> int:
        """Maakt of wijzigt een extern apparaat (bv. een Tuya-schakelaar). Elke gang met een lamp wordt een licht."""
        gangs = _gangs(gangs)
        if vid is None:
            vid = next((i for i in range(VIRTUAL_MIN, VIRTUAL_MAX + 1) if i not in self.virtual), None)
            if vid is None:
                raise ModelError("geen vrije id's voor externe apparaten meer")
        else:
            _int(vid, VIRTUAL_MIN, VIRTUAL_MAX, "id")
        used = {g["entity"] for v, gs in self.virtual.items() if v != vid for g in gs}
        for g in gangs:
            if g["entity"] in used:
                raise ModelError(f"{g['entity']} hoort al bij een ander extern apparaat")
        _conflicts(self.groups, {**self.virtual, vid: gangs})
        self.virtual[vid] = gangs
        n = self.nodes.setdefault(vid, {"name": "", "room": "", "adopted": True})
        n["name"], n["room"], n["adopted"] = _name(name, "name") or n["name"], _name(room, "room"), True
        for i, g in enumerate(gangs):
            key = (vid, i)
            if g["lamp"]:
                l = self.lights.get(key) or Light(vid, i, enabled=False)  # standaard verborgen: local-tuya heeft de entiteit al
                l.name = g["name"] or l.name
                self.lights[key] = l
            else:
                self.lights.pop(key, None)
        for key in [k for k in self.lights if k[0] == vid and k[1] >= len(gangs)]:
            del self.lights[key]
        self._purge_sources(vid, len(gangs))
        self._changed()
        return vid

    def remove_virtual(self, vid: int) -> bool:
        if vid not in self.virtual:
            return False
        del self.virtual[vid]
        self.nodes.pop(vid, None)
        for key in [k for k in self.lights if k[0] == vid]:
            del self.lights[key]
        for key in [k for k in self.follow if k[0] == vid or (self.follow[k] and self.follow[k][0] == vid)]:
            del self.follow[key]
        self._purge_sources(vid, 0)
        self._changed()
        return True

    def _purge_sources(self, vid: int, keep: int) -> None:
        """Haalt knoppen van een extern apparaat (vanaf index `keep`) uit alle triggerlijsten."""
        for l in self.lights.values():
            l.triggers = [t for t in l.triggers if not (t.node == vid and t.btn >= keep)]

    def set_follow(self, node: int, ind: int, light: tuple[int, int] | None | str) -> None:
        """Indicator `ind` van `node`: een licht, None (uit) of "auto" (afgeleid uit de triggers)."""
        _int(node, 1, 0xEFFF, "node")
        _int(ind, 0, MAX_IND - 1, "ind")
        if light == "auto":
            self.follow.pop((node, ind), None)
        else:
            self.follow[(node, ind)] = light
        self.adopt_quiet(node)
        self._changed()

    # ------------------------------------------------------------ wat een node hoort te hebben

    def desired_relays(self, node: int, nrelays: int) -> list[list[tuple[int, int, int]]]:
        out = []
        for r in range(nrelays):
            l = self.lights.get((node, r))
            out.append([t.tuple() for t in l.triggers] if l else [])
        return out

    def auto_follow(self, node: int, ind: int) -> tuple[int, int] | None:
        """Indicator i hoort bij knop i: het eerste licht (op volgorde) dat die knop bedient."""
        for key in sorted(self.lights):
            if any(t.node == node and t.btn == ind for t in self.lights[key].triggers):
                return key
        return None

    def effective_follow(self, node: int, ind: int) -> tuple[int, int] | None:
        if (node, ind) in self.follow:
            return self.follow[(node, ind)]
        return self.auto_follow(node, ind)

    def desired_follows(self, node: int, nind: int) -> list[tuple[int, int] | None]:
        return [self.effective_follow(node, i) for i in range(nind)]
