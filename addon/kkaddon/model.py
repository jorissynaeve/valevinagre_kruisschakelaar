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
                         "disable": "nacht"}},                                  # optioneel: timer of module (m:<id>) die de touches blokkeert; "control": bron die de lamp aan of uit zet en houdt
   "timers": {"nacht": {"name": "Nacht", "days": {"mon": [{"from": "03:00", "to": "07:00"}]}}},  # per dag tot 3 intervallen; "to" 00:00 = middernacht
   "modules": {"donker": {"type": "between", "name": "Donker", "inputs": {"from": "m:avond", "to": "m:ochtend"}},   # tijd en logica, zie logic.py
               "pauze": {"type": "time", "name": "Pauze", "value": "02:30", "inputs": {}}},
   "sprinklers": {"tuin": {"name": "Tuin", "enabled": false, "start": "06:00", "cycles": 2, "wait": 5, "refresh": 30, "days": [0, 1, 2, 3, 4, 5, 6],  # wait in minuten, refresh in seconden
                           "inputs": {"start": "m:avond", "block": "t:regen"},
                           "zones": [{"name": "Gazon", "entity": "switch.klep_1", "seconds": 120}]}},
   "plan":   {"floors": [{"id": "g", "name": "Begane grond", "w": 1000, "h": 640,
                          "rooms": [{"x": 20, "y": 20, "w": 300, "h": 200, "name": "Living"}],
                          "items": {"n5": [120, 80], "l2.1": [200, 90]}}]},   # plattegrond, alleen weergave
   "schema": {"pages": [{"id": "keuken", "name": "Keuken"}],                    # schema: één pagina per ruimte, alleen weergave
              "blocks": {"g:keuken": {"page": "keuken", "x": 420, "y": 40}},    # g:<kring>, l<node>.<relais>, n<node>, d:<schakelaar>, p:<paneel>
              "switches": {"abc": {"name": "T4B001", "gangs": [{"entity": "switch.a", "name": "relay_1"}]}},
              "names": {"d:abc": "Touch keuken"}}}                              # eigen naam van een schakelaar-blok, naast de technische
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import cards as _cards
from .link import GANG_REF_RE, split_ref
from .logic import BOOL, MODULE_TYPES, NUMBER, SPRINKLER_INPUTS, TIME, disable_ref
from .logic import minutes as _minutes
from .hasp import DOMAINS as SCREEN_DOMAINS
from .hasp import MAX_BUTTONS as HASP_MAX_BUTTONS
from .hasp import MAX_PAGES as HASP_MAX_PAGES
from .panel import PanelError
from .proto import MAX_GANGS, MAX_IND, MAX_RELAYS, MAX_TRIG, MODE_BY_NAME, VIRTUAL_MAX, VIRTUAL_MIN, is_virtual

log = logging.getLogger("kk.model")

MAX_NAME = 60
MAX_FLOORS, MAX_ROOMS, MAX_ITEMS, PLAN_MAX = 8, 80, 400, 4000
MAX_GROUPS, MAX_GROUP_ENTITIES = 120, 40
MAX_PAGES, MAX_BLOCKS, MAX_SWITCHES, MAX_SWITCH_GANGS, SCHEMA_MAX = 60, 1000, 300, 64, 20000
MAX_TIMERS, MAX_INTERVALS = 60, 3
MAX_MODULES = 200
MAX_SPRINKLERS, MAX_ZONES, MAX_CYCLES, MAX_WAIT, MAX_ZONE_SECONDS, MIN_REFRESH, MAX_REFRESH = 20, 16, 100, 1440, 86400, 5, 600
MAX_NUMBER = 100000
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FORMAT_VERSION = 1
MAX_PANELS = 8
MAX_PANEL_YAML = 100_000
MAX_PANEL_CARDS = 16
MAX_CARDS = 100
MAX_NOTES, MAX_NOTE = 60, 300
PANEL_HOST_RE = re.compile(r"[A-Za-z0-9.\-]{1,253}")
CARD_ID_RE = re.compile(r"[a-z0-9_]{1,40}")
ICON_RE = re.compile(r"[a-z0-9-]{1,60}")
TUYA_ID_RE = re.compile(r"[a-z0-9_]{1,80}")
WIFI_SECURITY = ("WPA", "WEP", "nopass")
MAX_SCREENS = 8
MAX_SCREEN_PAGES = HASP_MAX_PAGES
MAX_SCREEN_BUTTONS = HASP_MAX_BUTTONS
MAX_SCREEN_TIMEOUT = 3600
SCREEN_PLATE_RE = re.compile(r"[A-Za-z0-9_\-]{1,32}")  # komt in een MQTT-topic: geen / + of #
SCREEN_ENTITY_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")


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
AIRCO_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")  # zoals greehub.REF_RE
FLOOR_ID_RE = re.compile(r"^[a-z0-9]{1,12}$")
GROUP_ID_RE = re.compile(r"^[a-z0-9_]{1,80}$")
DISABLE_RE = re.compile(r"^(m:)?[a-z0-9_]{1,80}$")
# een bron: een timer, een module, of rechtstreeks een datapunt van een Tuya-toestel (v:<datapunt>@<toestel>, hier of r:..:y:<id>)
SOURCE_RE = re.compile(r"^([tm]:[a-z0-9_]{1,80}|v:\d{1,4}@([a-z0-9_]{1,80}|r:[a-z0-9_]{1,40}:y:[a-z0-9_]{1,80})|w:[a-z0-9_]{1,40}:(on|room|set)|o:[a-z0-9_]{1,40}:[a-z0-9_]{1,12})$")  # w: een uitgang van een airco (greehub.py), o: van een omvormer (solarhub.py)
VALUE_RE = re.compile(r"^v:\d{1,4}@([a-z0-9_]{1,80}|r:[a-z0-9_]{1,40}:y:[a-z0-9_]{1,80})$")  # alleen een datapunt


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
        if not isinstance(e, str) or not (ENTITY_RE.match(e) or GANG_REF_RE.match(e)):  # r:<installatie>:e:<entiteit> = een gang van een andere installatie
            raise ModelError(f"kring {gid}: ongeldige entiteit {e!r}")
        if e in ents:
            raise ModelError(f"kring {gid}: {e} komt twee keer voor")
        ents.append(e)
    lamp = v.get("lamp") or []
    if not isinstance(lamp, list) or any(not isinstance(x, str) or x not in ents for x in lamp):
        raise ModelError(f"kring {gid}: lamp-relais moeten entiteiten van de kring zijn")
    out = {"name": _name(v.get("name"), "kring.name") or gid, "enabled": bool(v.get("enabled", False)), "entities": ents,
           "lamp": [e for e in ents if e in lamp]}
    for key in ("disable", "control"):
        src = v.get(key)
        if src is not None:
            if not isinstance(src, str) or not DISABLE_RE.match(src):
                raise ModelError(f"kring {gid}: ongeldige bron {src!r}")
            out[key] = src
    return out


def _groups(raw) -> dict[str, dict]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_GROUPS:
        raise ModelError(f"kringen: maximaal {MAX_GROUPS}")
    return {gid: _group(gid, v) for gid, v in raw.items()}


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


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


def _module(mid, v) -> dict:
    if not isinstance(mid, str) or not GROUP_ID_RE.match(mid) or not isinstance(v, dict):
        raise ModelError(f"module: ongeldig id {mid!r}")
    kind = v.get("type")
    if kind not in MODULE_TYPES:
        raise ModelError(f"module {mid}: onbekend type {kind!r}")
    name = _name(v.get("name"), "module.name")
    if not name:
        raise ModelError("geef de module een naam")
    raw = v.get("inputs") or {}
    known = {i[0] for i in MODULE_TYPES[kind]["inputs"]}
    if not isinstance(raw, dict) or set(raw) - known:
        raise ModelError(f"module {name}: onbekende ingang")
    inputs = {}
    for k, ref in raw.items():
        if ref is None:
            continue
        if not isinstance(ref, str) or not SOURCE_RE.match(ref):
            raise ModelError(f"module {name}: ongeldige bron {ref!r}")
        inputs[k] = ref
    out = {"type": kind, "name": name, "inputs": inputs}
    if kind == "time":
        value = v.get("value", "00:00")
        if not isinstance(value, str) or not TIME_RE.match(value):
            raise ModelError("module: tijd verwacht als UU:MM")
        out["value"] = value
    if kind == "number":
        out["value"] = _int(v.get("value", 1), 0, MAX_NUMBER, "getal")
    if kind in ("above", "below"):
        out["value"] = _num(v.get("value", 0), -MAX_NUMBER, MAX_NUMBER, "grens")
    if kind == "dp":
        out["device"], out["dp"] = _device_ref(v.get("device")), _dp(v.get("dp"))
    return out


def _device_ref(v) -> str:
    """Een Tuya-toestel: `<id>` van deze installatie of `r:<installatie>:y:<id>`; leeg = nog niet gekozen."""
    v = v or ""
    remote = split_ref(v) if isinstance(v, str) else None
    if not isinstance(v, str) or (v and not TUYA_ID_RE.fullmatch(v) and not (remote and remote[1].startswith("y:"))):
        raise ModelError("ongeldig Tuya-toestel")
    return v


def _dp(v) -> str:
    v = "" if v is None else str(v)
    if v and not re.fullmatch(r"\d{1,4}", v):
        raise ModelError("datapunt: een nummer, bv. 109")
    return v


MAX_ACTIONS = 40


def _action(aid, v) -> dict:
    """Tuya instellen (zie actions.py): een 0/1 zet een datapunt. Staat standaard uit."""
    if not isinstance(aid, str) or not GROUP_ID_RE.match(aid) or not isinstance(v, dict):
        raise ModelError(f"routine: ongeldig id {aid!r}")
    raw = v.get("inputs") or {}
    if not isinstance(raw, dict) or set(raw) - {"trigger"}:
        raise ModelError("routine: onbekende ingang")
    ref = raw.get("trigger")
    if ref is not None and (not isinstance(ref, str) or not SOURCE_RE.match(ref)):
        raise ModelError(f"routine: ongeldige bron {ref!r}")
    vals = {}
    for k in ("on_value", "off_value"):
        x = v.get(k, "")
        if not isinstance(x, (str, int, float)) or isinstance(x, bool) or len(str(x)) > 40:
            raise ModelError("routine: een waarde van hoogstens 40 tekens")
        vals[k] = str(x).strip()
    return {"name": _name(v.get("name"), "routine.name") or aid, "enabled": bool(v.get("enabled", False)), "device": _device_ref(v.get("device")),
            "dp": _dp(v.get("dp")), **vals, "interval": _int(v.get("interval", 60), 10, 86400, "interval"), "inputs": {"trigger": ref} if ref else {}}


def _actions(raw) -> dict[str, dict]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_ACTIONS:
        raise ModelError(f"routines: maximaal {MAX_ACTIONS}")
    return {k: _action(k, v) for k, v in raw.items()}


def _check_actions(actions: dict[str, dict], modules: dict[str, dict], timers: dict[str, dict]) -> None:
    for a in actions.values():
        ref = a["inputs"].get("trigger")
        if ref is None:
            continue
        got = _source_type(ref, modules, timers)
        if got is None:
            raise ModelError(f"routine {a['name']}: de ingang wijst naar iets dat niet bestaat")
        if got != BOOL:
            raise ModelError(f"routine {a['name']}: de ingang verwacht een 0 of 1")


def _modules(raw) -> dict[str, dict]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_MODULES:
        raise ModelError(f"modules: maximaal {MAX_MODULES}")
    return {k: _module(k, v) for k, v in raw.items()}


_SOORT = {BOOL: "een 0 of 1", TIME: "een tijd", NUMBER: "een getal"}


def _sprinkler(sid, v) -> dict:
    if not isinstance(sid, str) or not GROUP_ID_RE.match(sid) or not isinstance(v, dict):
        raise ModelError(f"sprinkler: ongeldig id {sid!r}")
    name = _name(v.get("name"), "sprinkler.name")
    if not name:
        raise ModelError("geef de sprinkler een naam")
    start = v.get("start", "06:00")
    if not isinstance(start, str) or not TIME_RE.match(start):
        raise ModelError("sprinkler: startuur verwacht als UU:MM")
    days = v.get("days", list(range(7)))
    if not isinstance(days, list) or any(isinstance(d, bool) or d not in range(7) for d in days):
        raise ModelError("sprinkler: onbekende dag")
    raw = v.get("inputs") or {}
    if not isinstance(raw, dict) or set(raw) - {i[0] for i in SPRINKLER_INPUTS}:
        raise ModelError(f"sprinkler {name}: onbekende ingang")
    inputs = {}
    for k, ref in raw.items():
        if ref is None:
            continue
        if not isinstance(ref, str) or not SOURCE_RE.match(ref):
            raise ModelError(f"sprinkler {name}: ongeldige bron {ref!r}")
        inputs[k] = ref
    rawz = v.get("zones")
    if not isinstance(rawz, list) or not 1 <= len(rawz) <= MAX_ZONES:
        raise ModelError(f"een sprinkler heeft 1 tot {MAX_ZONES} zones")
    zones, seen = [], set()
    for i, z in enumerate(rawz):
        if not isinstance(z, dict):
            raise ModelError("zone moet een object zijn")
        ent = z.get("entity")
        if ent is not None:
            if not isinstance(ent, str) or not ENTITY_RE.match(ent):
                raise ModelError(f"zone {i + 1}: ongeldige entiteit {ent!r}")
            if ent in seen:
                raise ModelError(f"{ent} staat op twee zones")
            seen.add(ent)
        zones.append({"name": _name(z.get("name"), "zone.name") or f"Zone {i + 1}", "entity": ent, "seconds": _int(z.get("seconds", 60), 0, MAX_ZONE_SECONDS, "seconden")})
    return {"name": name, "enabled": bool(v.get("enabled", False)), "start": start, "days": sorted(set(days)), "inputs": inputs, "zones": zones,
            "cycles": _int(v.get("cycles", 1), 1, MAX_CYCLES, "cycli"), "wait": _int(v.get("wait", 5), 0, MAX_WAIT, "wachttijd"),
            "refresh": _int(v.get("refresh", 30), MIN_REFRESH, MAX_REFRESH, "herhalen")}


def panel_relay_entity(pid: str, n: int) -> str:
    """De entiteit van een relais van een NSPanel in HA (`default_entity_id` in de discovery)."""
    return f"switch.kk_panel_{pid}_relay{n}"


def _panel(pid, v) -> dict:
    """Een paneel: adres, de kaarten in volgorde (een screensaver-kaart is het startscherm) en de instellingen van het scherm.
    `notes` zijn meldingen van het omzetten van een oude YAML, tot iemand ze wegklikt."""
    if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", pid) or not isinstance(v, dict):
        raise ModelError(f"paneel: ongeldig id {pid!r}")
    name = _name(v.get("name"), "paneel.name") or pid
    host = v.get("host") or ""
    if not isinstance(host, str) or (host.strip() and not PANEL_HOST_RE.fullmatch(host.strip())):
        raise ModelError("paneel: geen geldig IP-adres of hostnaam")
    hub = v.get("hub") or ""  # het adres van Home Assistant zoals de panel het ziet; leeg = het adres waarmee je de add-on opende
    if not isinstance(hub, str) or (hub.strip() and not PANEL_HOST_RE.fullmatch(hub.strip())):
        raise ModelError("paneel: geen geldig adres voor Home Assistant")
    raw = v.get("cards") or []
    if not isinstance(raw, list) or len(raw) > MAX_PANEL_CARDS or any(not isinstance(c, str) or not CARD_ID_RE.fullmatch(c) for c in raw):
        raise ModelError(f"paneel: maximaal {MAX_PANEL_CARDS} kaarten")
    if len(set(raw)) != len(raw):
        raise ModelError("paneel: een kaart staat er twee keer op")
    locale = v.get("locale", "nl")
    if locale not in ("nl", "en"):
        raise ModelError("paneel: taal nl of en")
    notes = v.get("notes") or []
    if not isinstance(notes, list) or len(notes) > MAX_NOTES or any(not isinstance(n, str) for n in notes):
        raise ModelError("paneel: ongeldige meldingen")
    return {"name": name, "host": host.strip(), "hub": hub.strip(), "cards": list(raw), "locale": locale, "timeout": _int(v.get("timeout", 20), 0, 3600, "paneel.timeout"),
            "bright": _int(v.get("bright", 100), 0, 100, "paneel.bright"), "dim": _int(v.get("dim", 20), 0, 100, "paneel.dim"), "notes": [n[:MAX_NOTE] for n in notes]}


def _yaml_panel(pid, v, cards: dict[str, dict]) -> tuple[dict, dict[str, dict], str | None]:
    """Tot 0.11 had een paneel een YAML van nspanel-lovelace-ui. Die wordt eenmalig kaarten. Geeft (paneel, nieuwe kaarten, de oude YAML of None)."""
    text = v.get("yaml") if isinstance(v, dict) else None
    if not isinstance(text, str) or not text.strip() or v.get("cards"):
        return _panel(pid, {k: x for k, x in (v or {}).items() if k != "yaml"} if isinstance(v, dict) else v), {}, None
    base = {"name": v.get("name"), "host": v.get("host"), "locale": "en"}  # zonder locale was het scherm Engels: zo blijft het
    try:
        new, fields, notes = _cards.from_yaml(text, set(cards))
        new = {k: _card(k, c) for k, c in new.items()}
        msgs = ["Omgezet uit de YAML van nspanel-lovelace-ui; de oude YAML staat in de datamap.", *notes]
    except (PanelError, ModelError) as e:
        new, fields, msgs = {}, {}, [f"De YAML kon niet worden omgezet ({e}). Ze staat in de datamap; hang de kaarten met de hand aan het paneel."]
    return _panel(pid, {**base, **fields, "notes": msgs[:MAX_NOTES]}), new, text


def _check_panels(panels: dict[str, dict], cards: dict[str, dict]) -> None:
    for p in panels.values():
        missing = [c for c in p["cards"] if c not in cards]
        if missing:
            raise ModelError(f"paneel {p['name']}: onbekende kaart {missing[0]}")
        if sum(1 for c in p["cards"] if cards[c]["type"] == "saver") > 1:
            raise ModelError(f"paneel {p['name']}: één screensaver per paneel")


def _num(v, lo, hi, what) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ModelError(f"{what}: verwacht een getal tussen {lo} en {hi}, kreeg {v!r}")
    return v


def _opt_entity(v, what) -> str | None:
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not ENTITY_RE.match(v):
        raise ModelError(f"{what}: ongeldige entiteit {v!r}")
    return v


def _icon_name(v) -> str:
    if v in (None, ""):
        return ""
    v = v.removeprefix("mdi:") if isinstance(v, str) else v
    if not isinstance(v, str) or not ICON_RE.fullmatch(v):
        raise ModelError(f"pictogram: verwacht een MDI-naam zoals lightbulb, kreeg {v!r}")
    return v


def _plain(v, what, n: int = MAX_NAME) -> str:
    """Tekst die op het scherm komt: een ~ scheidt daar de velden."""
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > n or "~" in v:
        raise ModelError(f"{what}: tekst van maximaal {n} tekens zonder ~ verwacht")
    return v


def _status_list(v) -> list[str]:
    """Statuswaarden van een laadpaal (bv. connect, charing): een lijst, of tekst met komma's. Leeg = de status beslist niet."""
    if v is None or v == "":
        return []
    xs = v.split(",") if isinstance(v, str) else v
    if not isinstance(xs, list) or len(xs) > 20:
        raise ModelError("laadpaal: hoogstens 20 statuswaarden")
    out: list[str] = []
    for x in xs:
        if not isinstance(x, (str, int, float)) or isinstance(x, bool) or len(str(x).strip()) > 40:
            raise ModelError("laadpaal: een statuswaarde van hoogstens 40 tekens")
        s = str(x).strip()
        if s and s.lower() not in (o.lower() for o in out):
            out.append(s)
    return out


def _card(cid, v) -> dict:
    """Een kaart voor een NSPanel (zie cards.py)."""
    if not isinstance(cid, str) or not CARD_ID_RE.fullmatch(cid) or not isinstance(v, dict):
        raise ModelError(f"kaart: ongeldig id {cid!r}")
    t = v.get("type")
    if t not in _cards.TYPES:
        raise ModelError(f"kaart: onbekend type {t!r}")
    out: dict = {"type": t, "name": _plain(_name(v.get("name"), "kaart.name"), "kaart.name") or _cards.TYPES[t]}
    if t == "grid":
        layout = v.get("layout", "grid")
        if layout not in _cards.LAYOUTS:
            raise ModelError("kaart: weergave raster of lijst")
        items = v.get("items") or []
        if not isinstance(items, list) or len(items) > _cards.MAX_ITEMS[layout]:
            raise ModelError(f"kaart {out['name']}: maximaal {_cards.MAX_ITEMS[layout]} knoppen")
        clean = []
        for it in items:
            if not isinstance(it, dict):
                raise ModelError("kaart: ongeldige knop")
            ref, ent, dp = it.get("ref") or None, _opt_entity(it.get("entity"), "knop"), _dp(it.get("dp"))
            if dp:  # een waarde van een Tuya-toestel: y:<id> hier, of r:<installatie>:y:<id>
                if not isinstance(ref, str) or not _cards.DEVICE_REF.match(ref):
                    raise ModelError("kaart: een waarde komt van een Tuya-toestel")
            elif ref is not None and (not isinstance(ref, str) or not (_cards.LAMP_REF.match(ref) or _cards.CARD_REF.match(ref))):
                raise ModelError(f"kaart: een knop kan niet naar {ref!r} wijzen")
            if ref and ent:
                raise ModelError("kaart: een knop bedient een lamp óf een entiteit")
            if ref == f"c:{cid}":
                raise ModelError("kaart: een knop kan niet naar zijn eigen kaart gaan")
            clean.append({"ref": ref, "entity": ent, "name": _plain(_name(it.get("name"), "knop.name"), "knop.name"), "icon": _icon_name(it.get("icon")), "dp": dp})
        out.update(layout=layout, items=clean)
    elif t == "charger":
        ins, tgt = v.get("inputs") or {}, v.get("target") or {}
        if v.get("tuya"):  # tot 0.15 koos je een Feyree-lader: dat zijn deze datapunten (zie tuyahub.FEYREE)
            dev = _device_ref(v["tuya"])
            ins = {"status": f"v:101@{dev}", "time": f"v:120@{dev}", "energy": f"v:25@{dev}"}
            tgt = {"device": dev, "dp": "124", "on": "OpenCharging", "off": "CloseCharging"}
        if not isinstance(ins, dict) or set(ins) - set(_cards.CHARGER_INPUTS) or not isinstance(tgt, dict):
            raise ModelError("laadpaal: onbekende ingang")
        for k, r in ins.items():
            if r is not None and (not isinstance(r, str) or not VALUE_RE.match(r)):
                raise ModelError(f"laadpaal: {k} moet een datapunt van een Tuya-toestel zijn")
        vals = {}
        for k in ("on", "off"):
            x = tgt.get(k, "")
            if not isinstance(x, (str, int, float)) or isinstance(x, bool) or len(str(x)) > 40:
                raise ModelError("laadpaal: een waarde van hoogstens 40 tekens")
            vals[k] = str(x).strip()
        whens = {k: _status_list(v.get(k)) for k in ("start_when", "stop_when")}
        both = {s.lower() for s in whens["start_when"]} & {s.lower() for s in whens["stop_when"]}
        if both:
            raise ModelError(f"laadpaal: de status {sorted(both)[0]!r} kan niet starten én stoppen")
        out.update(inputs={k: r for k, r in ins.items() if r}, target={"device": _device_ref(tgt.get("device")), "dp": _dp(tgt.get("dp")), **vals}, **whens,
                   price=_num(v.get("price", 0.30), 0, 100, "prijs per kWh"), price_entity=_opt_entity(v.get("price_entity"), "prijs"))
    elif t == "airco":
        ac = v.get("airco") or ""
        if not isinstance(ac, str) or (ac and not AIRCO_ID_RE.fullmatch(ac)):
            raise ModelError("airco: ongeldige airco")
        out["airco"] = ac  # het id van de airco-module (greehub); leeg = nog niet gekozen
    elif t == "saver":
        out.update({k: _opt_entity(v.get(k), k) for k in ("weather", "forecast", "temperature", "status1", "status2")})
    elif t == "power":
        items = v.get("items") or []
        if not isinstance(items, list) or len(items) > _cards.MAX_POWER:
            raise ModelError(f"energie: maximaal {_cards.MAX_POWER} rijen")
        clean = []
        for it in items:
            if not isinstance(it, dict):
                raise ModelError("energie: ongeldige rij")
            lv = it.get("levels")
            if lv is not None and (not isinstance(lv, list) or len(lv) != 2 or _num(lv[0], 0, MAX_NUMBER, "grens") >= _num(lv[1], 0, MAX_NUMBER, "grens")):
                raise ModelError("energie: twee oplopende grenzen voor de kleur")
            ent, dev, dp = _opt_entity(it.get("entity"), "energie"), it.get("tuya") or "", _dp(it.get("dp"))
            if dev and (not isinstance(dev, str) or not _cards.DEVICE_REF.match(dev) or not dp):
                raise ModelError("energie: kies een Tuya-toestel en een datapunt")
            if dev and ent:
                raise ModelError("energie: een rij toont een sensor óf een Tuya-datapunt")
            clean.append({"entity": ent, "tuya": dev, "dp": dp if dev else "", "name": _plain(_name(it.get("name"), "rij.name"), "rij.name"), "icon": _icon_name(it.get("icon")),
                          "speed": _int(it.get("speed", 0), -120, 120, "snelheid"), "levels": lv})
        out["items"] = clean
    elif t == "qr":
        sec = v.get("security", "WPA")
        if sec not in WIFI_SECURITY:
            raise ModelError("wifi: beveiliging WPA, WEP of nopass")
        out.update(ssid=_plain(v.get("ssid") or "", "netwerknaam", 32), password=_plain(v.get("password") or "", "wachtwoord", 63), security=sec)
    return out


def _check_cards(cards: dict[str, dict]) -> None:
    """Een knop naar een subpagina wijst naar een kaart die bestaat en geen screensaver is."""
    for c in cards.values():
        for it in c.get("items", []) if c["type"] == "grid" else []:
            ref = it.get("ref") or ""
            if ref.startswith("c:"):
                sub = cards.get(ref[2:])
                if sub is None:
                    raise ModelError(f"kaart {c['name']}: de subpagina bestaat niet")
                if sub["type"] == "saver":
                    raise ModelError(f"kaart {c['name']}: een screensaver kan geen subpagina zijn")


def _load_cards(raw) -> dict[str, dict]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_CARDS:
        raise ModelError(f"kaarten: maximaal {MAX_CARDS}")
    return {k: _card(k, v) for k, v in raw.items()}


def _panels(raw, cards: dict[str, dict]) -> tuple[dict[str, dict], dict[str, dict], dict[str, str]]:
    """(panelen, alle kaarten, oude YAML's per paneel om te bewaren)."""
    if raw is None:
        return {}, cards, {}
    if not isinstance(raw, dict) or len(raw) > MAX_PANELS:
        raise ModelError(f"panelen: maximaal {MAX_PANELS}")
    out, cards, old = {}, dict(cards), {}
    for k, v in raw.items():
        p, new, text = _yaml_panel(k, v, cards)
        if len(cards) + len(new) > MAX_CARDS:
            raise ModelError(f"kaarten: maximaal {MAX_CARDS}")
        cards.update(new)
        out[k] = p
        if text is not None:
            old[k] = text
    return out, cards, old


def _screen(sid, v) -> dict:
    """Een scherm met openHASP: zoals een paneel een lijst kaarten in volgorde (zie hasp.py), zonder screensaver."""
    if not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", sid) or not isinstance(v, dict):
        raise ModelError(f"scherm: ongeldig id {sid!r}")
    plate = v.get("plate") or ""
    if not isinstance(plate, str) or (plate.strip() and not SCREEN_PLATE_RE.fullmatch(plate.strip())):
        raise ModelError("scherm: de naam op de firmware mag alleen letters, cijfers, - en _ bevatten")
    raw = v.get("cards") or []
    if not isinstance(raw, list) or len(raw) > MAX_PANEL_CARDS or any(not isinstance(c, str) or not CARD_ID_RE.fullmatch(c) for c in raw):
        raise ModelError(f"scherm: maximaal {MAX_PANEL_CARDS} kaarten")
    if len(set(raw)) != len(raw):
        raise ModelError("scherm: een kaart staat er twee keer op")
    locale = v.get("locale", "nl")
    if locale not in ("nl", "en"):
        raise ModelError("scherm: taal nl of en")
    return {"name": _name(v.get("name"), "scherm.name") or sid, "plate": plate.strip(), "timeout": _int(v.get("timeout", 30), 0, MAX_SCREEN_TIMEOUT, "scherm.timeout"),
            "cards": list(raw), "locale": locale}


def _old_screen(sid, v, cards: dict[str, dict]) -> tuple[dict, dict[str, dict]]:
    """Tot 0.16 had een scherm eigen pagina's met knoppen. Elke pagina wordt eenmalig een rasterkaart. Geeft (scherm, nieuwe kaarten)."""
    pages = v.get("pages") if isinstance(v, dict) else None
    rest = {k: x for k, x in v.items() if k != "pages"} if isinstance(v, dict) else v
    if not isinstance(pages, list) or (isinstance(v, dict) and v.get("cards")):
        return _screen(sid, rest), {}
    name = _name(v.get("name"), "scherm.name") or sid
    taken, new, order = set(cards), {}, []
    for i, page in enumerate(pages[:MAX_SCREEN_PAGES]):
        if not isinstance(page, dict):
            continue
        title = str(page.get("title") or (name if len(pages) == 1 else f"{name} {i + 1}")).replace("~", "-")[:MAX_NAME]
        base = re.sub(r"[^a-z0-9_]+", "_", title.lower()).strip("_")[:30] or "kaart"
        cid, n = base, 2
        while cid in taken:
            cid, n = f"{base}_{n}", n + 1
        taken.add(cid)
        items = []
        for b in (page.get("buttons") or [])[:MAX_SCREEN_BUTTONS]:
            b = b if isinstance(b, dict) else {}
            ent = b.get("entity")
            ok = isinstance(ent, str) and SCREEN_ENTITY_RE.fullmatch(ent) and ent.split(".")[0] in SCREEN_DOMAINS
            items.append({"ref": None, "entity": ent if ok else None, "name": str(b.get("label") or "").replace("~", "-").strip()[:MAX_NAME], "icon": ""})
        new[cid] = _card(cid, {"type": "grid", "name": title, "layout": "grid", "items": items})
        order.append(cid)
    return _screen(sid, {**rest, "cards": order}), new


def _check_screens(screens: dict[str, dict], cards: dict[str, dict]) -> None:
    for s in screens.values():
        missing = [c for c in s["cards"] if c not in cards]
        if missing:
            raise ModelError(f"scherm {s['name']}: onbekende kaart {missing[0]}")
        if any(cards[c]["type"] not in _cards.SCREEN_TYPES for c in s["cards"]):
            raise ModelError(f"scherm {s['name']}: een screensaver kan niet op een scherm met openHASP")


def _screens(raw, cards: dict[str, dict]) -> tuple[dict[str, dict], dict[str, dict], dict[str, list[str]]]:
    """(schermen, alle kaarten, de kaarten die per scherm uit oude pagina's zijn gemaakt)."""
    if raw in (None, {}):
        return {}, cards, {}
    if not isinstance(raw, dict) or len(raw) > MAX_SCREENS:
        raise ModelError(f"schermen: maximaal {MAX_SCREENS}")
    out, cards, made = {}, dict(cards), {}
    for k, v in raw.items():
        s, new = _old_screen(k, v, cards)
        if len(cards) + len(new) > MAX_CARDS:
            raise ModelError(f"kaarten: maximaal {MAX_CARDS}")
        cards.update(new)
        out[k] = s
        if new:
            made[k] = list(new)
    _unique_plates(out)
    return out, cards, made


def _unique_plates(screens: dict[str, dict]) -> None:
    plates = [s["plate"] for s in screens.values() if s["plate"]]
    for p in plates:
        if plates.count(p) > 1:
            raise ModelError(f"scherm {p} staat al op een ander blok: een scherm heeft één eigenaar")


def _sprinklers(raw) -> dict[str, dict]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_SPRINKLERS:
        raise ModelError(f"sprinklers: maximaal {MAX_SPRINKLERS}")
    return {k: _sprinkler(k, v) for k, v in raw.items()}


def _check_sprinklers(sprinklers: dict[str, dict], modules: dict[str, dict], timers: dict[str, dict]) -> None:
    for s in sprinklers.values():
        for name, want, label in SPRINKLER_INPUTS:
            ref = s["inputs"].get(name)
            if ref is None:
                continue
            got = _source_type(ref, modules, timers)
            if got is None:
                raise ModelError(f"sprinkler {s['name']}: ingang {label} wijst naar iets dat niet bestaat")
            if got != want:
                raise ModelError(f"sprinkler {s['name']}: ingang {label} verwacht {_SOORT[want]}")


def source_type(ref: str, modules: dict[str, dict], timers: dict[str, dict]) -> str | None:
    return _source_type(ref, modules, timers) if isinstance(ref, str) and SOURCE_RE.match(ref) else None


def _source_type(ref: str, modules: dict[str, dict], timers: dict[str, dict]) -> str | None:
    """Wat een bron geeft: een tijd, een 0/1, of None als ze niet bestaat."""
    if ref.startswith("o:"):
        return NUMBER  # een omvormer staat niet in het model: is ze weg of offline, dan is de waarde onbekend, geen fout
    if ref.startswith("w:"):
        return BOOL if ref.endswith(":on") else NUMBER  # een airco staat niet in het model: is ze weg, dan is de waarde onbekend, geen fout
    if ref.startswith("v:"):
        return NUMBER  # een datapunt: een getal (aan/uit = 1/0); is het toestel er niet, dan is de waarde onbekend, geen fout
    if ref.startswith("t:"):
        return BOOL if ref[2:] in timers else None
    m = modules.get(ref[2:])
    return MODULE_TYPES[m["type"]]["out"] if m else None


def _check_modules(modules: dict[str, dict], timers: dict[str, dict]) -> None:
    """Elke ingang wijst naar iets dat bestaat en van de juiste soort is, en een uitgang komt nooit bij zichzelf terug."""
    for m in modules.values():
        for name, want, label in MODULE_TYPES[m["type"]]["inputs"]:
            ref = m["inputs"].get(name)
            if ref is None:
                continue
            got = _source_type(ref, modules, timers)
            if got is None:
                raise ModelError(f"module {m['name']}: ingang {label} wijst naar iets dat niet bestaat")
            if got != want:
                raise ModelError(f"module {m['name']}: ingang {label} verwacht {_SOORT[want]}")
    done: set[str] = set()

    def walk(mid: str, path: set[str]) -> None:
        if mid in path:
            raise ModelError(f"module {modules[mid]['name']}: deze verbinding maakt een lus")
        if mid in done:
            return
        for ref in modules[mid]["inputs"].values():
            if ref.startswith("m:"):
                walk(ref[2:], path | {mid})
        done.add(mid)

    for mid in modules:
        walk(mid, set())


def _check_disable(disable, modules: dict[str, dict], timers: dict[str, dict]) -> None:
    if not isinstance(disable, str) or not DISABLE_RE.match(disable):
        raise ModelError(f"ongeldige bron: {disable!r}")
    got = _source_type(disable_ref(disable), modules, timers)
    if got is None:
        raise ModelError("onbekende module" if disable.startswith("m:") else "onbekende timer")
    if got != BOOL:
        raise ModelError("Deze ingang heeft een 0 of 1 nodig, geen tijd. Zet er een Tussen, Na of Voor tussen.")


def _conflicts(groups: dict[str, dict], virtual: dict[int, list[dict]], sprinklers: dict[str, dict] | None = None) -> None:
    """Een entiteit mag maar door één ding worden bestuurd, anders werken die elkaar tegen."""
    owner: dict[str, str] = {}
    for vid, gangs in virtual.items():
        for g in gangs:
            owner.setdefault(g["entity"], f"extern apparaat {vid}")
    valves: dict[str, str] = {}  # een extern apparaat mag: dat is alleen de bron van de gang, geen tweede die bevelen geeft
    for s in (sprinklers or {}).values():
        if s["enabled"]:
            for z in s["zones"]:
                if z["entity"] in valves:
                    raise ModelError(f"{z['entity']} wordt al bestuurd door {valves[z['entity']]} en staat ook op sprinkler {s['name']}")
                if z["entity"]:
                    valves[z["entity"]] = f"sprinkler {s['name']}"
    for e, who in valves.items():
        owner.setdefault(e, who)
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
BLOCK_RE = re.compile(r"^(g:[a-z0-9_]{1,80}|d:[a-z0-9_]{1,80}|t:[a-z0-9_]{1,80}|m:[a-z0-9_]{1,80}|s:[a-z0-9_]{1,80}|p:[a-z0-9_]{1,80}|h:[a-z0-9_]{1,80}|y:[a-z0-9_]{1,80}|w:[a-z0-9_]{1,40}|o:[a-z0-9_]{1,40}|c:[a-z0-9_]{1,40}|a:[a-z0-9_]{1,80}|r:[a-z0-9_]{1,40}:(?:g:[a-z0-9_]{1,80}|l\d{1,5}\.\d{1,2}|y:[a-z0-9_]{1,80}|e:[a-z0-9_]{1,60}\.[a-z0-9_]{1,190})|n\d{1,5}|l\d{1,5}\.\d{1,2})$")


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
        self.modules: dict[str, dict] = {}  # tijd- en logicamodules: id -> {type, name, inputs, value}
        self.sprinklers: dict[str, dict] = {}  # sprinklers: id -> {name, enabled, start, cycles, wait, refresh, days, inputs, zones}
        self.panels: dict[str, dict] = {}  # NSPanels: id -> {name, host, cards, locale, timeout, bright, dim, notes}
        self.cards: dict[str, dict] = {}  # kaarten voor de NSPanels: id -> {type, name, ...} (zie cards.py)
        self.actions: dict[str, dict] = {}  # Tuya instellen: id -> {name, enabled, device, dp, on_value, off_value, interval, inputs}
        self.screens: dict[str, dict] = {}  # schermen met openHASP: id -> {name, plate, timeout, cards, locale}
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
        if self.modules:
            d["modules"] = self.modules
        if self.sprinklers:
            d["sprinklers"] = self.sprinklers
        if self.panels:
            d["panels"] = self.panels
        if self.cards:
            d["cards"] = self.cards
        if self.actions:
            d["actions"] = self.actions
        if self.screens:
            d["screens"] = self.screens
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
        modules = _modules(d.get("modules"))
        sprinklers = _sprinklers(d.get("sprinklers"))
        panels, cards, old_yaml = _panels(d.get("panels"), _load_cards(d.get("cards")))
        screens, cards, old_pages = _screens(d.get("screens"), cards)
        actions = _actions(d.get("actions"))
        if save:  # bij het opstarten nooit weigeren, bij een import wel
            _conflicts(groups, virtual, sprinklers)
            _check_modules(modules, timers)
            _check_sprinklers(sprinklers, modules, timers)
            _check_cards(cards)
            _check_panels(panels, cards)
            _check_screens(screens, cards)
            _check_actions(actions, modules, timers)
            for g in groups.values():
                for key in ("disable", "control"):
                    if g.get(key, "").startswith("m:"):
                        _check_disable(g[key], modules, timers)
        else:  # wat niet (meer) klopt valt weg in plaats van het opstarten te breken
            panels = {k: {**p, "cards": [c for c in p["cards"] if c in cards]} for k, p in panels.items()}
            screens = {k: {**s, "cards": [c for c in s["cards"] if c in cards and cards[c]["type"] in _cards.SCREEN_TYPES]} for k, s in screens.items()}
            for c in cards.values():
                if c["type"] == "grid":
                    c["items"] = [it for it in c["items"] if not (it["ref"] or "").startswith("c:") or (it["ref"][2:] in cards and cards[it["ref"][2:]]["type"] != "saver")]
        self.nodes, self.lights, self.follow, self.virtual, self.plan, self.groups, self.schema, self.timers = nodes, lights, follow, virtual, plan, groups, schema, timers
        self.modules, self.sprinklers, self.panels, self.cards, self.screens, self.actions = modules, sprinklers, panels, cards, screens, actions
        if old_yaml:
            self._keep_yaml(old_yaml)
            for pid in old_yaml:
                made = {c for cs in old_pages.values() for c in cs}
                self.place_cards_near(pid, [c for c in cards if c not in (d.get("cards") or {}) and c not in made])
        for sid, cids in old_pages.items():
            self.place_cards_near(sid, cids, block=f"h:{sid}")
            log.info("scherm %s: %d pagina('s) omgezet naar kaarten", sid, len(cids))
        if save:
            self._changed()
        elif old_yaml or old_pages:
            self.save()  # de omzetting meteen bewaren: anders gebeurt ze bij elke start opnieuw

    def place_cards_near(self, pid: str, cids: list[str], block: str | None = None) -> None:
        """Nieuwe kaarten in een kolom links van het paneel (of het scherm), als dat op het schema staat. Alleen weergave."""
        pos = self.schema["blocks"].get(block or f"p:{pid}")
        todo = [c for c in cids if f"c:{c}" not in self.schema["blocks"]]
        if not pos or not todo:
            return
        blocks = dict(self.schema["blocks"])
        y = pos["y"]
        for c in todo:  # de hoogtes zoals schema.py en schema.js ze tekenen (42 kop, 24 per rij, 10 voet), met 24 ertussen
            card = self.cards[c]
            rows = (len(card["items"]) + 1 if card["type"] == "grid" else max(len(card["items"]), 1) if card["type"] == "power" else 3 if card["type"] == "saver" else 2)
            blocks[f"c:{c}"] = {"page": pos["page"], "x": max(0, pos["x"] - 460), "y": min(SCHEMA_MAX, y)}  # rechts ervan plaats voor de verwijzingen
            y += 42 + rows * 24 + 10 + 24
        self.schema = {**self.schema, "blocks": dict(sorted(blocks.items()))}

    def _keep_yaml(self, old: dict[str, str]) -> None:
        """De oude YAML naast network.json bewaren: het omzetten laat dingen weg die je misschien nog wilt nakijken."""
        if not self.path:
            return
        for pid, text in old.items():
            target = self.path.parent / f"panel-{pid}.yaml.overgenomen"
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                log.info("paneel %s: YAML omgezet naar kaarten, de oude staat in %s", pid, target)
            except OSError as e:
                log.error("paneel %s: oude YAML niet bewaard in %s: %s", pid, target, e)

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
                                    "disable": old.get("disable") if old else None, "control": old.get("control") if old else None})
        if len(new) > MAX_GROUPS:
            raise ModelError(f"kringen: maximaal {MAX_GROUPS}")
        _conflicts(new, self.virtual, self.sprinklers)
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

    def set_group(self, gid: str, name=None, entities=None, enabled=None, lamp=None, disable=..., control=...) -> None:
        old = self.groups.get(gid)
        if old is None:
            raise ModelError("onbekende kring")
        ents = old["entities"] if entities is None else entities
        if lamp is None:  # nieuwe entiteitenlijst: lamp-relais die er niet meer in staan vallen weg
            lamp = [x for x in old["lamp"] if isinstance(ents, list) and x in ents]
        if disable is ...:
            disable = old.get("disable")
        elif disable is not None:
            _check_disable(disable, self.modules, self.timers)
        if control is ...:
            control = old.get("control")
        elif control is not None:
            _check_disable(control, self.modules, self.timers)
        new = _group(gid, {
            "disable": disable,
            "control": control,
            "name": old["name"] if name is None else name,
            "entities": ents,
            "enabled": old["enabled"] if enabled is None else enabled,
            "lamp": lamp,
        })
        groups = {**self.groups, gid: new}
        _conflicts(groups, self.virtual, self.sprinklers)
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
        self.groups = {k: {x: y for x, y in g.items() if not (x in ("disable", "control") and y == tid)} for k, g in self.groups.items()}
        self.modules = self._without_source(f"t:{tid}")
        self.sprinklers = self._sprinklers_without(f"t:{tid}")
        self.actions = self._actions_without(f"t:{tid}")
        self._changed()
        return True

    def _sprinklers_without(self, ref: str) -> dict[str, dict]:
        return {k: {**s, "inputs": {i: r for i, r in s["inputs"].items() if r != ref}} for k, s in self.sprinklers.items()}

    def add_sprinkler(self, name=None, zones: int = 3) -> str:
        """Een nieuwe sprinkler staat uit, zoals een nieuwe lamp: er gaat geen klep open voor je dat zelf kiest."""
        if len(self.sprinklers) >= MAX_SPRINKLERS:
            raise ModelError(f"sprinklers: maximaal {MAX_SPRINKLERS}")
        base = _name(name, "name") or "Sprinkler"
        slug = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")[:70] or "sprinkler"
        sid, label, n = slug, base, 2
        while sid in self.sprinklers:
            sid, label, n = f"{slug}_{n}", f"{base} {n}", n + 1
        self.sprinklers = {**self.sprinklers, sid: _sprinkler(sid, {"name": label, "zones": [{} for _ in range(_int(zones, 1, MAX_ZONES, "zones"))]})}
        self._changed()
        return sid

    def set_sprinkler(self, sid: str, **fields) -> None:
        """Alleen wat je meegeeft verandert. In `inputs` maakt None een ingang los."""
        old = self.sprinklers.get(sid)
        if old is None:
            raise ModelError("onbekende sprinkler")
        unknown = set(fields) - {"name", "enabled", "start", "cycles", "wait", "refresh", "days", "inputs", "zones"}
        if unknown:
            raise ModelError(f"sprinkler: onbekend veld {sorted(unknown)[0]}")
        if "inputs" in fields:
            if not isinstance(fields["inputs"], dict):
                raise ModelError("sprinkler: inputs moet een object zijn")
            fields["inputs"] = {**old["inputs"], **fields["inputs"]}
        new = {**self.sprinklers, sid: _sprinkler(sid, {**old, **fields})}
        _check_sprinklers(new, self.modules, self.timers)
        _conflicts(self.groups, self.virtual, new)
        self.sprinklers = new
        self._changed()

    def remove_sprinkler(self, sid: str) -> bool:
        if sid not in self.sprinklers:
            return False
        self.sprinklers = {k: v for k, v in self.sprinklers.items() if k != sid}
        self.schema = {**self.schema, "blocks": {k: v for k, v in self.schema["blocks"].items() if k != f"s:{sid}"}}
        self._changed()
        return True

    def add_panel(self, name=None, host="") -> str:
        if len(self.panels) >= MAX_PANELS:
            raise ModelError(f"panelen: maximaal {MAX_PANELS}")
        base = _name(name, "name") or "NSPanel"
        slug = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")[:30] or "nspanel"
        pid, label, n = slug, base, 2
        while pid in self.panels:
            pid, label, n = f"{slug}_{n}", f"{base} {n}", n + 1
        self.panels = {**self.panels, pid: _panel(pid, {"name": label, "host": host})}
        self._changed()
        return pid

    def set_panel(self, pid: str, **fields) -> None:
        """Alleen wat je meegeeft verandert. `cards` is de hele lijst in volgorde."""
        old = self.panels.get(pid)
        if old is None:
            raise ModelError("onbekend paneel")
        unknown = set(fields) - {"name", "host", "hub", "cards", "locale", "timeout", "bright", "dim", "notes"}
        if unknown:
            raise ModelError(f"paneel: onbekend veld {sorted(unknown)[0]}")
        if isinstance(fields.get("cards"), list):  # de screensaver vooraan: hij is geen pagina, dus de volgorde van de pagina's blijft
            fields["cards"] = sorted(fields["cards"], key=lambda c: not (isinstance(c, str) and self.cards.get(c, {}).get("type") == "saver"))
        panels = {**self.panels, pid: _panel(pid, {**old, **fields})}
        _check_panels(panels, self.cards)
        self.panels = panels
        self._changed()

    def import_panel_yaml(self, pid: str, text) -> list[str]:
        """Een YAML van nspanel-lovelace-ui omzetten naar kaarten en achteraan aan dit paneel hangen. Geeft de meldingen terug."""
        old = self.panels.get(pid)
        if old is None:
            raise ModelError("onbekend paneel")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_PANEL_YAML:
            raise ModelError(f"plak een YAML van maximaal {MAX_PANEL_YAML} tekens")
        try:
            new, fields, notes = _cards.from_yaml(text, set(self.cards))
        except PanelError as e:
            raise ModelError(str(e)) from e
        new = {k: _card(k, c) for k, c in new.items()}
        if len(self.cards) + len(new) > MAX_CARDS:
            raise ModelError(f"kaarten: maximaal {MAX_CARDS}")
        cards = {**self.cards, **new}
        order = [c for c in old["cards"] if not (cards[c]["type"] == "saver" and any(cards[x]["type"] == "saver" for x in fields["cards"]))]  # de nieuwe screensaver wint
        merged = sorted(order + fields["cards"], key=lambda c: cards[c]["type"] != "saver")
        panel = _panel(pid, {**old, **{k: v for k, v in fields.items() if k != "cards"}, "cards": merged})
        _check_cards(cards)
        panels = {**self.panels, pid: panel}
        _check_panels(panels, cards)
        self.cards, self.panels = cards, panels
        self._changed()
        return notes

    def _actions_without(self, ref: str) -> dict[str, dict]:
        return {k: {**a, "inputs": {i: r for i, r in a["inputs"].items() if r != ref}} for k, a in self.actions.items()}

    def add_action(self, name=None) -> str:
        """Een nieuwe routine staat uit: er wordt niets ingesteld voor je dat zelf kiest."""
        if len(self.actions) >= MAX_ACTIONS:
            raise ModelError(f"routines: maximaal {MAX_ACTIONS}")
        base = _name(name, "name") or "Tuya instellen"
        slug = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")[:70] or "routine"
        aid, label, n = slug, base, 2
        while aid in self.actions:
            aid, label, n = f"{slug}_{n}", f"{base} {n}", n + 1
        self.actions = {**self.actions, aid: _action(aid, {"name": label})}
        self._changed()
        return aid

    def set_action(self, aid: str, **fields) -> None:
        """Alleen wat je meegeeft verandert. In `inputs` maakt None een ingang los."""
        old = self.actions.get(aid)
        if old is None:
            raise ModelError("onbekende routine")
        unknown = set(fields) - {"name", "enabled", "device", "dp", "on_value", "off_value", "interval", "inputs"}
        if unknown:
            raise ModelError(f"routine: onbekend veld {sorted(unknown)[0]}")
        if "inputs" in fields:
            if not isinstance(fields["inputs"], dict):
                raise ModelError("routine: inputs moet een object zijn")
            fields["inputs"] = {k: r for k, r in {**old["inputs"], **fields["inputs"]}.items() if r is not None}
        new = {**self.actions, aid: _action(aid, {**old, **fields})}
        _check_actions(new, self.modules, self.timers)
        self.actions = new
        self._changed()

    def remove_action(self, aid: str) -> bool:
        if aid not in self.actions:
            return False
        self.actions = {k: v for k, v in self.actions.items() if k != aid}
        self.schema = {**self.schema, "blocks": {k: v for k, v in self.schema["blocks"].items() if k != f"a:{aid}"}}
        self._changed()
        return True

    def add_card(self, kind, name=None) -> str:
        if kind not in _cards.TYPES:
            raise ModelError(f"onbekende kaart {kind!r}")
        if len(self.cards) >= MAX_CARDS:
            raise ModelError(f"kaarten: maximaal {MAX_CARDS}")
        base = _name(name, "name") or _cards.TYPES[kind]
        slug = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")[:30] or "kaart"
        cid, label, n = slug, base, 2
        names = {c["name"] for c in self.cards.values()}
        while cid in self.cards or (not name and label in names):
            cid, label, n = f"{slug}_{n}", f"{base} {n}", n + 1
        self.cards = {**self.cards, cid: _card(cid, {"type": kind, "name": label})}
        self._changed()
        return cid

    def set_card(self, cid: str, **fields) -> None:
        """Alleen wat je meegeeft verandert; `items` is de hele lijst."""
        old = self.cards.get(cid)
        if old is None:
            raise ModelError("onbekende kaart")
        unknown = set(fields) - (set(old) - {"type"}) - ({"tuya"} if old["type"] == "charger" else set())  # tuya: een Feyree-lader in één keer
        if unknown:
            raise ModelError(f"kaart: onbekend veld {sorted(unknown)[0]}")
        cards = {**self.cards, cid: _card(cid, {**old, **fields})}
        _check_cards(cards)
        _check_panels(self.panels, cards)  # bv. een tweede screensaver kan niet: maar het type verandert hier nooit
        _check_screens(self.screens, cards)
        self.cards = cards
        self._changed()

    def remove_card(self, cid: str) -> bool:
        """Ook weg van de panelen, en de knoppen die erheen gingen verdwijnen."""
        if cid not in self.cards:
            return False
        ref = f"c:{cid}"
        self.cards = {k: ({**c, "items": [it for it in c["items"] if it.get("ref") != ref]} if c["type"] == "grid" else c) for k, c in self.cards.items() if k != cid}
        self.panels = {k: {**p, "cards": [c for c in p["cards"] if c != cid]} for k, p in self.panels.items()}
        self.screens = {k: {**s, "cards": [c for c in s["cards"] if c != cid]} for k, s in self.screens.items()}
        self.schema = {**self.schema, "blocks": {k: v for k, v in self.schema["blocks"].items() if k != ref}}
        self._changed()
        return True

    def remove_panel(self, pid: str) -> bool:
        if pid not in self.panels:
            return False
        self.panels = {k: v for k, v in self.panels.items() if k != pid}
        self.schema = {**self.schema, "blocks": {k: v for k, v in self.schema["blocks"].items() if k != f"p:{pid}"}}
        self._changed()
        return True

    def add_screen(self, name=None, plate="") -> str:
        if len(self.screens) >= MAX_SCREENS:
            raise ModelError(f"schermen: maximaal {MAX_SCREENS}")
        base = _name(name, "name") or "Scherm"
        slug = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")[:30] or "scherm"
        sid, label, n = slug, base, 2
        while sid in self.screens:
            sid, label, n = f"{slug}_{n}", f"{base} {n}", n + 1
        new = {**self.screens, sid: _screen(sid, {"name": label, "plate": plate})}
        _unique_plates(new)
        self.screens = new
        self._changed()
        return sid

    def set_screen(self, sid: str, **fields) -> None:
        """Alleen wat je meegeeft verandert. `cards` is de hele lijst in volgorde."""
        old = self.screens.get(sid)
        if old is None:
            raise ModelError("onbekend scherm")
        unknown = set(fields) - {"name", "plate", "timeout", "cards", "locale"}
        if unknown:
            raise ModelError(f"scherm: onbekend veld {sorted(unknown)[0]}")
        new = {**self.screens, sid: _screen(sid, {**old, **fields})}
        _unique_plates(new)
        _check_screens(new, self.cards)
        self.screens = new
        self._changed()

    def remove_screen(self, sid: str) -> bool:
        if sid not in self.screens:
            return False
        self.screens = {k: v for k, v in self.screens.items() if k != sid}
        self.schema = {**self.schema, "blocks": {k: v for k, v in self.schema["blocks"].items() if k != f"h:{sid}"}}
        self._changed()
        return True

    def _without_source(self, ref: str) -> dict[str, dict]:
        return {k: {**m, "inputs": {i: r for i, r in m["inputs"].items() if r != ref}} for k, m in self.modules.items()}

    def add_module(self, kind, name=None, value=None) -> str:
        """Zonder naam krijgt een module de naam van haar type: En, En 2, ..."""
        if kind not in MODULE_TYPES:
            raise ModelError(f"onbekend type module {kind!r}")
        if len(self.modules) >= MAX_MODULES:
            raise ModelError(f"modules: maximaal {MAX_MODULES}")
        given = _name(name, "name")
        base = given or MODULE_TYPES[kind]["label"]
        slug = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")[:70] or "module"
        mid, label, n = slug, base, 2
        names = {m["name"] for m in self.modules.values()}
        while mid in self.modules or (not given and label in names):
            mid, label, n = f"{slug}_{n}", f"{base} {n}", n + 1
        v = {"type": kind, "name": base if given else label, "inputs": {}}
        if value is not None:
            v["value"] = value
        self.modules = {**self.modules, mid: _module(mid, v)}
        self._changed()
        return mid

    def set_module(self, mid: str, name=None, value=None, inputs=None, **extra) -> None:
        """Alleen wat je meegeeft verandert. In `inputs` maakt None een ingang los. `extra`: `device` en `dp` van een Tuya-waarde."""
        old = self.modules.get(mid)
        if old is None:
            raise ModelError("onbekende module")
        if inputs is not None and not isinstance(inputs, dict):
            raise ModelError("module: inputs moet een object zijn")
        v = {**old, "inputs": {**old["inputs"], **(inputs or {})}}
        if name is not None:
            v["name"] = name
        if value is not None:
            v["value"] = value
        if set(extra) - {"device", "dp"}:
            raise ModelError(f"module: onbekend veld {sorted(set(extra) - {'device', 'dp'})[0]}")
        v.update(extra)
        modules = {**self.modules, mid: _module(mid, v)}
        _check_modules(modules, self.timers)
        _check_actions(self.actions, modules, self.timers)  # een module die nu iets anders geeft dan een 0/1
        self.modules = modules
        self._changed()

    def remove_module(self, mid: str) -> bool:
        """Wat haar gebruikte is daarna niet meer aangesloten; een lamp is dan gewoon weer te bedienen."""
        if mid not in self.modules:
            return False
        ref = f"m:{mid}"
        self.modules = self._without_source(ref)
        self.sprinklers = self._sprinklers_without(ref)
        self.actions = self._actions_without(ref)
        self.modules.pop(mid)
        self.groups = {k: {x: y for x, y in g.items() if not (x in ("disable", "control") and y == ref)} for k, g in self.groups.items()}
        self.schema = {**self.schema, "blocks": {k: v for k, v in self.schema["blocks"].items() if k != ref}}
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
        _conflicts(self.groups, {**self.virtual, vid: gangs}, self.sprinklers)
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
