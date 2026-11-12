"""ESPHome-toestellen via hun eigen webserver (`web_server:` in de YAML) als HUISDEV-apparaat: zelfde blok `e:<id>`, zelfde punten en acties.

Het toestel hoeft niet aangepast te worden. Zijn webserver stuurt op `GET /events` (server-sent events) bij het verbinden van elke entiteit een
volledige regel (naam, eenheid, opties, bereik) en daarna alleen nog wijzigingen. Hier staat het pure deel: die regels lezen, er een descriptor van
maken zoals HUISDEV §5 hem beschrijft, de waarden eruit halen en een opdracht omzetten naar het REST-verzoek van ESPHome. Het netwerk en de opslag
zitten in espdevhub.py.

  sensor, binary_sensor, text_sensor     uitgang (float, bool, text)
  switch, light, fan                     bool, instelbaar (alleen aan/uit; helderheid en kleur laten we aan HA)
  select, number, text                   enum, float of int, text; instelbaar
  button                                 actie (restart, reset en dergelijke zijn `danger`)

Alles wat we niet kennen (cover, climate, lock, ...) wordt overgeslagen en staat als "niet ondersteund" in `skipped`, niet als fout.
Ids: een punt-id van HUISDEV is `[a-z][a-z0-9_]{0,23}`, een ESPHome-id kan anders zijn. De toewijzing (ESPHome-id -> punt-id) wordt bewaard in `emap`
en verandert nooit meer, ook niet als er later een entiteit met dezelfde naam bijkomt (HUISDEV §9).
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote

READ_DOMAINS = ("sensor", "binary_sensor", "text_sensor")
WRITE_DOMAINS = ("switch", "light", "fan", "select", "number", "text")
SUPPORTED = READ_DOMAINS + WRITE_DOMAINS + ("button",)
PID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
DANGER_RE = re.compile(r"restart|reboot|reset|factory|wipe|safe[_ -]?mode|shutdown|deep[_ -]?sleep", re.I)
MAX_OPTIONS = 64

# eenheid -> (class voor HA, state-hint). Alleen eenheden die niets anders kunnen betekenen: een `%` kan vocht of batterij zijn, dus dat laten we weg.
UNITS = {
    "W": ("power", "measurement"), "kW": ("power", "measurement"), "V": ("voltage", "measurement"), "mV": ("voltage", "measurement"), "A": ("current", "measurement"),
    "mA": ("current", "measurement"), "Wh": ("energy", "total_increasing"), "kWh": ("energy", "total_increasing"), "VA": ("apparent_power", "measurement"),
    "°C": ("temperature", "measurement"), "°F": ("temperature", "measurement"), "Hz": ("frequency", "measurement"), "hPa": ("pressure", "measurement"),
    "dBm": ("signal_strength", "measurement"), "lx": ("illuminance", "measurement"),
}


# ------------------------------------------------------------ de eventstroom lezen

class Sse:
    """Een server-sent-events-stroom in stukjes verwerkt: `feed(bytes)` geeft de volledige gebeurtenissen (soort, data) die er nu bij kwamen."""

    def __init__(self) -> None:
        self.buf = b""

    def feed(self, chunk: bytes) -> list[tuple[str, str]]:
        self.buf += chunk
        if len(self.buf) > 1 << 20:  # een stroom die nooit een lege regel stuurt is geen ESPHome
            self.buf = b""
            raise ValueError("gebeurtenis te groot")
        out = []
        while True:
            m = re.search(rb"\r?\n\r?\n", self.buf)
            if not m:
                return out
            raw, self.buf = self.buf[:m.start()], self.buf[m.end():]
            event, data = "message", []
            for line in raw.decode("utf-8", "replace").splitlines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
            out.append((event, "\n".join(data)))


def parse_event(event: str, data: str):
    """('ping', titel) of ('state', entiteit) of None. Logregels en alles wat niet JSON is, tellen niet mee."""
    if event == "ping":
        try:
            j = json.loads(data) if data.strip() else {}
        except ValueError:
            return ("ping", "")
        return ("ping", j.get("title", "") if isinstance(j, dict) and isinstance(j.get("title"), str) else "")
    if event != "state":
        return None
    try:
        j = json.loads(data)
    except ValueError:
        return None
    return ("state", j) if isinstance(j, dict) and isinstance(j.get("id"), str) else None


def split_id(eid: str) -> tuple[str, str] | None:
    """`sensor-power` -> ('sensor', 'power'). ESPHome gebruikt het stuk na het eerste streepje ook in zijn URL's."""
    domain, sep, obj = eid.partition("-")
    return (domain, obj) if sep and domain and obj and re.fullmatch(r"[a-z_]+", domain) else None


def slug(text: str, default: str = "esphome") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or default


# ------------------------------------------------------------ ids

def make_pid(eid: str, taken) -> str:
    domain, obj = split_id(eid) or ("x", eid)
    base = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", obj.lower())).strip("_") or "x"
    if not base[0].isalpha():
        base = "p_" + base
    for cand in (base[:24], f"{domain}_{base}"[:24]):
        if PID_RE.match(cand) and cand not in taken:
            return cand
    h = hashlib.sha1(eid.encode()).hexdigest()
    for n in range(4, 40):
        cand = f"{base[:19]}_{h[:n]}"[:24]
        if cand not in taken:
            return cand
    raise ValueError("geen vrij id")


# ------------------------------------------------------------ van entiteit naar punt of actie

def _decimals(state, fallback: int) -> int:
    """Het aantal decimalen dat ESPHome zelf toont ("232.2 V" -> 1): dat is zijn accuracy_decimals."""
    m = re.match(r"\s*-?\d+(?:\.(\d+))?", state) if isinstance(state, str) else None
    return fallback if not m else min(len(m.group(1) or ""), 6)


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) < 1e12 else None


def _label(e: dict, obj: str) -> str:
    name = e.get("name")
    return name.strip()[:60] if isinstance(name, str) and name.strip() else obj


def describe(eid: str, e: dict, prev: dict | None = None) -> tuple[str, dict] | None:
    """Een volledige entiteitsregel als ('point', punt zonder id) of ('action', actie zonder id), of None als we hem niet kennen.
    `prev` is het punt zoals het eerder was: als de tekst van het toestel geen getal heeft ("NA") houden we zijn decimalen aan."""
    sp = split_id(eid)
    if not sp or sp[0] not in SUPPORTED:
        return None
    domain, obj = sp
    label = _label(e, obj)
    cat = e.get("entity_category")
    base = {"label": label, "unit": "", "decimals": 0, "min": None, "max": None, "step": None, "options": [], "writable": False, "danger": False, "diag": cat == 2,
            "group": "instellingen" if cat == 1 else "", "cls": "", "state": "", "stale": 0, "confirm": ""}
    if domain == "button":
        danger = bool(DANGER_RE.search(f"{obj} {label}"))
        return "action", {"label": label, "danger": danger, "confirm": f"{label}: het toestel voert dit meteen uit. Doorgaan?" if danger else "", "args": []}
    if domain in ("binary_sensor", "switch", "light", "fan"):
        return "point", {**base, "kind": "bool", "writable": domain != "binary_sensor"}
    if domain in ("text_sensor", "text"):
        return "point", {**base, "kind": "text", "writable": domain == "text"}
    if domain == "select":
        opts = [{"v": i, "l": str(o)[:40]} for i, o in enumerate(e["option"][:MAX_OPTIONS])] if isinstance(e.get("option"), list) else []
        if not opts:
            return None
        return "point", {**base, "kind": "enum", "writable": True, "options": opts}
    uom = e.get("uom") if isinstance(e.get("uom"), str) else ""
    dec = _decimals(e.get("state"), prev["decimals"] if prev and prev["kind"] in ("float", "int") else 1)
    if domain == "sensor":
        cls, st = UNITS.get(uom, ("", "measurement"))
        return "point", {**base, "kind": "float", "unit": uom[:16], "decimals": dec, "cls": cls, "state": st}
    # number
    lo, hi, step = _num(e.get("min_value")), _num(e.get("max_value")), _num(e.get("step"))
    whole = dec == 0 and (step is None or step >= 1 and step == int(step)) and all(x is None or x == int(x) for x in (lo, hi))
    cls = UNITS.get(uom, ("", ""))[0]
    return "point", {**base, "kind": "int" if whole else "float", "writable": True, "unit": uom[:16], "decimals": dec, "cls": cls, "min": lo, "max": hi, "step": step}


def build(ents: dict[str, dict], emap: dict[str, str], prev_points: list[dict] | None = None) -> dict:
    """Alle volledige entiteitsregels -> een descriptor in de vorm van `clean_descriptor` (zonder device-velden), de nieuwe emap en de overgeslagen entiteiten.
    `emap` is punt-id -> ESPHome-id; bestaande toewijzingen blijven, nieuwe krijgen een vrij id."""
    by_eid = {eid: pid for pid, eid in emap.items()}
    prev = {p["id"]: p for p in prev_points or []}
    new_map = dict(emap)
    points, actions, skipped = [], [], []
    for eid, e in ents.items():  # in de volgorde van het toestel: zo staan zijn belangrijkste entiteiten ook vooraan in het standaardblok
        if "name" not in e:
            continue  # een deelregel ("alleen de waarde"): de volledige regel komt bij het verbinden
        got = describe(eid, e, prev.get(by_eid.get(eid, "")))
        if not got:
            skipped.append(eid)
            continue
        kind, d = got
        pid = by_eid.get(eid)
        if pid is None:
            pid = make_pid(eid, set(new_map) | {p["id"] for p in prev_points or []})
            by_eid[eid] = pid
        new_map[pid] = eid
        (points if kind == "point" else actions).append({"id": pid, **d})
    live = {p["id"] for p in points} | {a["id"] for a in actions}
    keep = {pid for pid in new_map if pid in live}
    body = json.dumps([points, actions], sort_keys=True, separators=(",", ":"))
    return {"points": points, "actions": actions, "emap": {pid: new_map[pid] for pid in keep}, "skipped": sorted(skipped), "rev": hashlib.sha1(body.encode()).hexdigest()[:8]}


def value_of(p: dict, e: dict | None):
    """De waarde van een punt volgens de laatste regels van zijn entiteit, van het type van het punt. Onbekend ("NA", ontbreekt) is None."""
    if not e:
        return None
    k = p["kind"]
    v, st = e.get("value"), e.get("state")
    if k == "bool":
        if isinstance(v, bool):
            return v
        return st == "ON" if st in ("ON", "OFF") else None
    if k == "text":
        s = v if isinstance(v, str) else st if isinstance(st, str) else None
        return s[:255] if s is not None and s != "NA" else None
    if k == "enum":
        if isinstance(v, str):
            names = e.get("option") if isinstance(e.get("option"), list) else []
            return next((i for i, o in enumerate(names) if str(o)[:40] == v[:40]), None)
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return int(round(v)) if k == "int" else float(v)


# ------------------------------------------------------------ van opdracht naar REST-verzoek

def point_request(eid: str, p: dict, ent: dict | None, value) -> tuple[str, dict]:
    """(pad, query) voor `POST` op de webserver van ESPHome. Opdrachten zijn absoluut: aan of uit, nooit toggle (HUISDEV §7)."""
    domain, obj = split_id(eid)
    o = quote(obj, safe="")
    if p["kind"] == "bool":
        return f"/{domain}/{o}/turn_{'on' if value else 'off'}", {}
    if p["kind"] == "enum":
        names = (ent or {}).get("option")
        if not isinstance(names, list) or not 0 <= value < len(names):
            raise ValueError("de keuzes van het toestel zijn onbekend")
        return f"/select/{o}/set", {"option": str(names[value])}
    if p["kind"] == "text":
        return f"/text/{o}/set", {"value": value}
    return f"/number/{o}/set", {"value": repr(float(value)) if isinstance(value, float) else str(value)}


def action_request(eid: str) -> tuple[str, dict]:
    domain, obj = split_id(eid)
    return f"/button/{quote(obj, safe='')}/press", {}
