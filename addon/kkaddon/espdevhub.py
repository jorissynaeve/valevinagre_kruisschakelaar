"""Zelfbouw-apparaten met het HUISDEV-protocol (protocol/huisdev/HUISDEV.md) als eigen blok `e:<id>`.

Een ESP met `huisdev.h` meldt zich op het LAN (UDP 47500), beschrijft zichzelf (punten en acties, met een `rev`) en de add-on maakt
er na een klik op "claimen" een blok van. Verandert de set punten, dan verandert de `rev` en past het blok zich aan.

  uitgangen   elk punt (behalve tekst en diagnose) is een bron `e:<id>:<punt>`: een 0/1 bij een bool, een getal bij int, float en enum
  ingangen    elk instelbaar punt dat niet `danger` is: een bron (timer, module, waarde) stuurt het punt, zoals bij de airco

Een ingang handelt op een **wijziging**, nooit op een stand (zoals greehub.py): de eerste stand na een herstart of na onbekend wordt
alleen onthouden. Bevelen zijn altijd absoluut. Wat `danger` is (reset, kalibratie) kan alleen met de hand, met bevestiging, uit de zijbalk.

Het id van het apparaat (`poolcontroller-a1b2c3`) heeft streepjes; hier heet het `poolcontroller_a1b2c3`, want een bron kent alleen `[a-z0-9_]`.
De toestellen en hun token staan in /data/espdev.json (0600), niet in het model: network.json gaat mee in de export. De status voor de
pagina bevat nooit het token.

Verdwijnt een punt, dan blijft hij 30 dagen "niet beschikbaar": de verwijzingen in de logica blijven staan en geven onbekend, en de
HA-entiteit wordt `unavailable`. Daarna (of bij verwijderen van het apparaat) worden de MQTT-topics gewist; die lijst staat in het bestand,
want opruimen in het geheugen overleeft een herstart niet.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import socket
import time
from pathlib import Path

import aiohttp

from . import discovery as d
from . import esphome as esp
from . import hue
from . import brink
from . import model as _model
from . import nuki
from . import smappee
from .engine import Engine
from .logic import BOOL, NUMBER
from .model import ModelError, fits, source_type

log = logging.getLogger("kk.espdev")

UDP_PORT = 47500
CURRENT = (1, 0)  # het protocol zoals de add-on het kent (protocol/huisdev/HUISDEV.md); een lagere minor van een apparaat toont "upgrade beschikbaar"
SUPPORTED_MAJORS = (1,)
MAX_DEVICES = 80  # een Hue-installatie alleen al heeft gauw dertig lampen
MAX_BRIDGES = 4
MAX_POINTS, MAX_ACTIONS, MAX_ARGS = 64, 16, 8
POLL = 3.0  # s
MAX_BODY = 65536  # bytes; een descriptor is hoogstens 8 KB, een state 4 KB (HUISDEV.md §5)
HTTP_TIMEOUT = 5  # s totaal per verzoek: een ESP over zwak wifi met een blokkerende webserver is traag
FAILS = 3  # zo vaak achter elkaar mislukt = offline
SCAN_EVERY = 60  # s: zolang een apparaat offline is of er niets gevonden is
FOUND_KEEP = 3600  # s: wat zich zo lang niet meer meldde, verdwijnt uit de keuzelijst
GONE_KEEP = 30 * 86400  # s: een verdwenen punt blijft zo lang "niet beschikbaar"
RETRY = 30.0  # s: een ingang die niet doorkwam, opnieuw proberen
TICK = 1.0
DEFAULT_SHOWN = 8
ES_FLUSH = 1.0  # s: ESPHome pusht elke wijziging (een vermogensmeting twee keer per s); hoogstens zo vaak verwerken we ze
ES_SETTLE = 1.0  # s na het verbinden: wacht tot de eerste stroom volledige regels binnen is, anders groeit de descriptor in stukjes
ES_PROBE = 6.0  # s: zolang wacht "toevoegen" op de eerste regels van een ESPHome-toestel
ES_READ = 35  # s zonder een byte (ESPHome stuurt elke ~10 s een ping) is de verbinding weg
HUE_FLUSH = 0.5  # s: hoogstens zo vaak verwerken we wat de eventstroom van de bridge bracht
HUE_PING = 30.0  # s: de eventstroom van een bridge zwijgt als er niets verandert (gemeten: geen hartslag in 3 minuten), dus vragen we zelf of hij er nog is
HUE_RESYNC = 600.0  # s: de hele lijst opnieuw ophalen, voor wat een gemiste gebeurtenis scheef zette
HUE_GAP = 0.1  # s tussen twee opdrachten aan een bridge: Hue verdraagt ongeveer 10 per seconde voor lampen
BRIDGE_RE = re.compile(r"^[a-z0-9_]{1,24}$")
MAX_ACCOUNTS = 3
NUKI_POLL = 30.0  # s: Nuki laat 100 verzoeken per minuut en 1000 per uur toe per token, en de token is misschien ook van iets anders; 120 per uur is zuinig genoeg
NUKI_BURST = (2.0, 6.0, 15.0)  # s: na een opdracht (die Nuki asynchroon uitvoert) snel nog een paar keer kijken
NUKI_BACKOFF = 90.0  # s wachten na "te veel verzoeken"
LID_RE = re.compile(r"^[0-9]{1,20}$")
MAX_BRINK = 3
BRINK_POLL = 30.0  # s: de module meldt zich ongeveer elke 8 s bij Brink; de API heeft geen gepubliceerde limiet, en dit is zuinig
BRINK_BURST = (2.0, 6.0, 15.0)  # s: na een opdracht (die Brink asynchroon uitvoert) snel nog een paar keer kijken
BRINK_BACKOFF = 90.0  # s na "te veel verzoeken"
BRINK_AUTH_WAIT = 900.0  # s: een onjuist wachtwoord proberen we niet steeds opnieuw (een account kan geblokkeerd raken); wijzig het wachtwoord om meteen opnieuw te proberen
BRINK_REAUTH = 60  # s: een toegangstoken dat nog zo lang geldig is, vernieuwen we al
MAX_SMAPPEE = 2
SMAPPEE_POLL = 60.0  # s: de laadstand uit de cloud van Smappee. Smappee publiceert geen limiet en de HA-integratie vraagt maar elke 5 tot 30 minuten; een minuut is zuinig genoeg voor een pauze
SMAPPEE_BURST = (3.0, 10.0, 30.0)  # s: na een opdracht (Smappee voert hem asynchroon uit) snel nog een paar keer kijken
SMAPPEE_BACKOFF = 120.0  # s na "te veel verzoeken", als Smappee zelf geen Retry-After geeft
SMAPPEE_AUTH_WAIT = 900.0  # s: een onjuist wachtwoord proberen we niet steeds opnieuw; wijzig het wachtwoord om meteen opnieuw te proberen
SMAPPEE_REAUTH = 60  # s: een toegangstoken dat nog zo lang geldig is, vernieuwen we al
SMAPPEE_STRUCT = 1800.0  # s: de lijst van laadstations en laadpunten verandert zelden
SMAPPEE_SESS_ACTIVE = 60.0  # s: de sessies opnieuw vragen zolang er geladen of gepauzeerd wordt
SMAPPEE_SESS_IDLE = 600.0  # s: en anders
SMAPPEE_WRITE_GAP = 5.0  # s: dezelfde opdracht (pauze, of de stroom) die sneller volgt, wordt geweigerd (een bron die flappert belast de cloud niet; een ingang probeert het later opnieuw). Per punt: pauzeren en daarna de stroom kan meteen
SERIAL_RE = smappee.SERIAL_RE
PUB_BUDGET = 14  # verzoeken per uur aan de officiele API die wij zelf hoogstens doen (Smappee laat er 20 toe; het token telt mee en JavaLox deelt misschien dezelfde client)
PUB_RATE_ACTIVE = 360.0  # s: het verbruik tijdens laden of pauze (een blok van Smappee is 5 minuten)
PUB_RATE_IDLE = 900.0  # s: en anders
PUB_METER_EVERY = 1800.0  # s: de teller; daarnaast eenmaal als een sessie net klaar is
PUB_RETRY = 600.0  # s: na een mislukking pas weer proberen
PUB_MIN_GAP = 300.0  # s: de teller na het einde van een sessie hoogstens zo vaak
PUB_STALE_RATE = 2400.0  # s: een verbruik dat zo oud is, is onbekend
PUB_STALE_METER = 7200.0  # s: idem voor de teller
PUB_HOLD_AUTH = 3600.0  # s: onjuiste sleutels proberen we niet steeds opnieuw
PUB_HOLD_RATE = 1800.0  # s: minstens zo lang wachten na "te veel verzoeken"
PUB_HOLD_MAINT = 900.0  # s: na onderhoud
ACT_PREFIX = "do_"  # in `dev["inputs"]`: een ingang die een actie start (do_unlock), naast de ingangen die een punt instellen
ACTION_GAP = 5.0  # s: een actie die een ingang start, hoogstens zo vaak (een bron die flappert opent geen deur drie keer)

KINDS = ("bool", "int", "float", "enum", "text")
ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
PID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
REF_RE = re.compile(r"^e:([a-z0-9_]{1,40}):([a-z][a-z0-9_]{0,23})$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
PROTO_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})$")
SET_RE = re.compile(rf"^{d.PREFIX}/esp/([a-z0-9_]+)/([a-z][a-z0-9_]*)/set$")
PRESS_RE = re.compile(rf"^{d.PREFIX}/esp/([a-z0-9_]+)/do/([a-z][a-z0-9_]*)/press$")
HA_CLASSES = {"temperature", "power", "energy", "voltage", "current", "battery", "humidity", "pressure", "ph", "duration", "frequency", "water", "volume", "distance",
              "signal_strength", "apparent_power", "power_factor", "speed", "weight", "illuminance", "carbon_dioxide", "moisture", "irradiance", "conductivity"}
DOMAINS = ("sensor", "binary_sensor", "switch", "select", "number", "button")

ERRORS = {  # foutcodes van het protocol (HUISDEV §4) in gewoon Nederlands
    "token": "het apparaat weigert het token: wis de claim op het apparaat en koppel opnieuw",
    "not_claimed": "het apparaat is niet geclaimd", "not_writable": "dit punt is niet instelbaar", "unknown_point": "het apparaat kent dit punt niet",
    "unknown_action": "het apparaat kent deze actie niet", "disabled": "deze actie kan nu niet", "confirm_required": "bevestiging nodig",
    "missing_value": "geen waarde", "missing_arg": "een argument ontbreekt", "bad_value": "ongeldige waarde", "bad_option": "geen toegestane keuze",
    "range": "buiten het bereik", "step": "past niet op de stap", "already_claimed": "het apparaat is al geclaimd (wis de claim op het apparaat zelf)",
}


class EspError(Exception):
    def __init__(self, status: int, code: str, reason: str = "") -> None:
        super().__init__(reason or ERRORS.get(code, code))
        self.status, self.code, self.reason = status, code, reason


# ------------------------------------------------------------ pure hulpen: lezen en controleren wat een apparaat meldt


def parse_proto(raw) -> tuple[int, int] | None:
    m = PROTO_RE.match(raw) if isinstance(raw, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def proto_state(raw) -> str:
    """ok, upgrade (een lagere minor dan de add-on kent: zie de upgrade-gids) of unsupported (onbekende major)."""
    v = parse_proto(raw)
    if not v or v[0] not in SUPPORTED_MAJORS:
        return "unsupported"
    return "upgrade" if v[0] == CURRENT[0] and v[1] < CURRENT[1] else "ok"


def _text(x, n: int) -> str:
    return x.strip()[:n] if isinstance(x, str) else ""


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) < 1e12 else None


def parse_discovery(data: bytes, addr) -> dict | None:
    """Het antwoord of de aankondiging van een apparaat (HUISDEV §3). Alles wat er niet uitziet als een apparaat is ruis."""
    try:
        j = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(j, dict) or not parse_proto(j.get("huisdev")) or not isinstance(j.get("id"), str) or not ID_RE.match(j["id"]):
        return None
    port = j.get("port", 80)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    return {"hid": j["id"], "ip": addr[0], "port": port, "proto": j["huisdev"], "type": _text(j.get("type"), 40), "name": _text(j.get("name"), 60),
            "fw": _text(j.get("fw"), 40), "rev": _text(j.get("rev"), 16), "claimed": j.get("claimed") is True}


def _point(raw, arg: bool = False) -> dict | None:
    """Een punt of argument uit de descriptor, gecontroleerd. Een onbekend `kind` of een ongeldig id: overslaan (minor-versies mogen dingen toevoegen)."""
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS or not isinstance(raw.get("id"), str) or not PID_RE.match(raw["id"]):
        return None
    kind = raw["kind"]
    opts = []
    if isinstance(raw.get("options"), list):
        for o in raw["options"][:64]:
            if isinstance(o, dict) and isinstance(o.get("v"), int) and not isinstance(o["v"], bool) and isinstance(o.get("l"), str):
                opts.append({"v": o["v"], "l": o["l"][:40]})
    if kind == "enum" and not opts:
        return None  # een keuze zonder keuzes is niet te gebruiken
    dec = raw.get("decimals")
    out = {"id": raw["id"], "label": _text(raw.get("label"), 60) or raw["id"], "kind": kind, "unit": _text(raw.get("unit"), 16),
           "decimals": dec if isinstance(dec, int) and not isinstance(dec, bool) and 0 <= dec <= 6 else (1 if kind == "float" else 0),
           "min": _num(raw.get("min")), "max": _num(raw.get("max")), "step": _num(raw.get("step")), "options": opts if kind == "enum" else []}
    if not arg:
        stale = raw.get("stale")
        out.update(writable=raw.get("writable") is True, danger=raw.get("danger") is True, diag=raw.get("diag") is True, group=_text(raw.get("group"), 30),
                   cls=_text(raw.get("class") or raw.get("cls"), 30), state=raw.get("state") if raw.get("state") in ("measurement", "total_increasing") else "",
                   stale=stale if isinstance(stale, int) and not isinstance(stale, bool) and stale > 0 else 0)
        out["confirm"] = _text(raw.get("confirm"), 200) if out["danger"] else ""
    return out


def clean_descriptor(raw) -> dict | None:
    """De descriptor van `GET /huisdev/descriptor`, gecontroleerd en afgeknot. None als het geen descriptor is."""
    if not isinstance(raw, dict) or not parse_proto(raw.get("proto")) or not isinstance(raw.get("rev"), str) or not 0 < len(raw["rev"]) <= 16:
        return None
    dev = raw.get("device")
    if not isinstance(dev, dict) or not isinstance(dev.get("id"), str) or not ID_RE.match(dev["id"]):
        return None
    points, seen = [], set()
    for r in (raw.get("points") if isinstance(raw.get("points"), list) else [])[:MAX_POINTS]:
        p = _point(r)
        if p and p["id"] not in seen:
            seen.add(p["id"])
            points.append(p)
    actions, seen = [], set()
    for r in (raw.get("actions") if isinstance(raw.get("actions"), list) else [])[:MAX_ACTIONS]:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not PID_RE.match(r["id"]) or r["id"] in seen:
            continue
        args = [a for a in (_point(x, True) for x in (r.get("args") if isinstance(r.get("args"), list) else [])[:MAX_ARGS]) if a]
        if isinstance(r.get("args"), list) and len(args) != len(r["args"][:MAX_ARGS]):
            continue  # een actie met een argument dat we niet begrijpen, kunnen we niet veilig uitvoeren
        seen.add(r["id"])
        danger = r.get("danger") is True
        actions.append({"id": r["id"], "label": _text(r.get("label"), 60) or r["id"], "danger": danger, "confirm": _text(r.get("confirm"), 200) if danger else "", "args": args})
    return {"proto": raw["proto"], "rev": raw["rev"], "id": dev["id"], "type": _text(dev.get("type"), 40), "name": _text(dev.get("name"), 60), "fw": _text(dev.get("fw"), 40),
            "lib": _text(dev.get("lib"), 20), "points": points, "actions": actions}


def descriptor_problem(raw) -> str:
    """Waarom `clean_descriptor` een beschrijving afkeurt, voor de melding aan de gebruiker."""
    if not isinstance(raw, dict):
        return "het is geen JSON-object"
    if not parse_proto(raw.get("proto")):
        return f"proto ontbreekt of is ongeldig ({raw.get('proto')!r})"
    rev = raw.get("rev")
    if not isinstance(rev, str) or not 0 < len(rev) <= 16:
        return f"rev ontbreekt of is ongeldig ({rev!r})"
    dev = raw.get("device")
    if not isinstance(dev, dict):
        return "het veld device ontbreekt"
    if not isinstance(dev.get("id"), str) or not ID_RE.match(dev["id"]):
        return f"device.id is ongeldig ({dev.get('id')!r}; toegestaan: kleine letters, cijfers en streepjes, tot 32 tekens)"
    return "onbekende reden"


def check_value(p: dict, value) -> float | int | bool | str:
    """Een waarde voor een punt of argument, volgens zijn type, keuzes, bereik en stap. ModelError als het niet past."""
    kind = p["kind"]
    if kind == "text":
        if not isinstance(value, str) or len(value) > 200:
            raise ModelError(f"{p['label']}: tekst van hoogstens 200 tekens")
        return value
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.upper() in ("ON", "OFF", "TRUE", "FALSE", "1", "0"):
            return value.upper() in ("ON", "TRUE", "1")
        raise ModelError(f"{p['label']}: aan of uit")
    if isinstance(value, bool):
        raise ModelError(f"{p['label']}: een getal")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ModelError(f"{p['label']}: een getal") from None
    if v != v or abs(v) > 1e12:
        raise ModelError(f"{p['label']}: een getal")
    if kind in ("int", "enum"):
        if abs(v - round(v)) > 1e-9:
            raise ModelError(f"{p['label']}: een geheel getal")
        v = int(round(v))
    if kind == "enum":
        if v not in {o["v"] for o in p["options"]}:
            raise ModelError(f"{p['label']}: geen toegestane keuze")
        return v
    if p["min"] is not None and v < p["min"] - 1e-9 or p["max"] is not None and v > p["max"] + 1e-9:
        raise ModelError(f"{p['label']}: tussen {p['min'] if p['min'] is not None else '…'} en {p['max'] if p['max'] is not None else '…'}")
    if p["step"] and p["step"] > 0:
        k = (v - (p["min"] or 0)) / p["step"]
        if abs(k - round(k)) > 1e-6:
            raise ModelError(f"{p['label']}: in stappen van {p['step']:g}")
    return v


def _snap(p: dict, v: float):
    """Een getal van een bron netjes op een punt: binnen het bereik, op de stap, en een geheel getal bij int en enum."""
    if p["min"] is not None:
        v = max(p["min"], v)
    if p["max"] is not None:
        v = min(p["max"], v)
    if p["step"] and p["step"] > 0:
        base = p["min"] or 0
        v = base + round((v - base) / p["step"]) * p["step"]
    return int(round(v)) if p["kind"] in ("int", "enum") else v


def _slug(hid: str) -> str:
    return hid.replace("-", "_")


class EspDevHub:
    def __init__(self, data_dir: Path, engine: Engine, port: int | None = None, poll: float | None = None) -> None:
        self.path = Path(data_dir) / "espdev.json"
        self.engine = engine
        self.port = port  # alleen voor tests: een andere UDP-poort dan 47500 (0 = een vrije)
        self.poll = poll or POLL
        self.changed = None  # de MQTT-brug
        self.devices: dict[str, dict] = {}
        self.retired: dict[str, float] = {}  # MQTT-topics die gewist moeten worden -> sinds wanneer (blijft bewaard, tot ze zeker weg zijn)
        self.found: dict[str, dict] = {}  # id van het apparaat -> wat het meldde
        self.values: dict[str, dict] = {}  # did -> {punt: {"v", "age", "at"}}
        self.off: dict[str, dict] = {}  # did -> {actie: reden}: uitgeschakelde acties volgens de state
        self.online: set[str] = set()
        self.fails: dict[str, int] = {}
        self.errors: dict[str, str] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.wake: dict[str, asyncio.Event] = {}
        self.seen: dict[str, dict] = {}  # did -> {punt: laatst gevolgde stand van de ingang}
        self._failed: dict[tuple[str, str], float] = {}
        self._inflight: set[tuple[str, str]] = set()
        self._act_last: dict[tuple[str, str], float] = {}  # (did, actie) -> wanneer een ingang hem het laatst startte
        self.last: dict[str, dict] = {}  # did -> {punt: wat er laatst gebeurde}, voor de zijbalk
        self.es: dict[str, dict] = {}  # did -> wat de eventstroom van een ESPHome-toestel tot nu bracht (esphome.py)
        self.bridges: dict[str, dict] = {}  # Hue-bridges: id -> {name, host, port, key, fp, bridge_id}; de sleutel staat alleen in dit bestand (hue.py)
        self.hue: dict[str, dict] = {}  # bridge -> wat de bridge tot nu meldde: {store: {id: resource}, open, online, error, ...}
        self.hue_desc: dict[str, dict] = {}  # did -> de descriptor van een Hue-apparaat volgens de laatste lijst van de bridge
        self.accounts: dict[str, dict] = {}  # Nuki-accounts: id -> {name, token}; de token staat alleen in dit bestand (nuki.py)
        self.nk: dict[str, dict] = {}  # account -> wat Nuki tot nu meldde: {locks: {id: slot}, online, error, burst}
        self.nuki_desc: dict[str, dict] = {}  # did -> de descriptor van een slot volgens de laatste lijst van Nuki
        self.brink_accounts: dict[str, dict] = {}  # Brink Home-accounts: id -> {name, email, password}; alleen in dit bestand (brink.py)
        self.bk: dict[str, dict] = {}  # account -> wat Brink tot nu meldde: {units, systems, token, online, error, auth, burst}
        self.brink_desc: dict[str, dict] = {}  # did -> de descriptor van een ventilatie-unit volgens de laatste lijst van Brink
        self.smappee_accounts: dict[str, dict] = {}  # Smappee-accounts: id -> {name, username, password}; alleen in dit bestand (smappee.py)
        self.sm: dict[str, dict] = {}  # account -> wat Smappee tot nu meldde: {units, stations, sessions, token, online, error, auth, burst}
        self.smappee_desc: dict[str, dict] = {}  # did -> de descriptor van een laadpunt volgens de laatste lijst van Smappee
        self._smappee_sent: dict[tuple[str, str], float] = {}
        self._pub_budgets: dict[str, smappee.Budget] = {}  # client-id -> hoeveel verzoeken aan de officiele API er dit uur zijn gedaan (bewaard in smappee_budget.json)
        self._hue_locks: dict[str, asyncio.Lock] = {}
        self._hue_sent: dict[str, float] = {}
        self._listen = None
        self._ask = None
        self._http: aiohttp.ClientSession | None = None
        self._stream_http: aiohttp.ClientSession | None = None  # zonder totaaltijd: een eventstroom duurt zolang het toestel leeft
        self.udp_port = port or UDP_PORT
        self._load()
        engine.espdev_view = self.status
        engine.espdev_found = self.found_view
        engine.espdev_value = self.output
        engine.hue_view = self.hue_view
        engine.nuki_view = self.nuki_view
        engine.brink_view = self.brink_view
        engine.smappee_view = self.smappee_view
        _model.esp_type = self.point_type

    # ------------------------------------------------------------ opslag

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError, AttributeError) as e:
            log.error("%s niet leesbaar: %s", self.path, e)
            return
        if not isinstance(raw, dict):
            return
        for k, v in (raw.get("bridges") or {}).items() if isinstance(raw.get("bridges"), dict) else []:
            br = self._bridge_defaults(v) if isinstance(v, dict) and BRIDGE_RE.match(k) else None
            if br:
                self.bridges[k] = br
        for k, v in (raw.get("nuki") or {}).items() if isinstance(raw.get("nuki"), dict) else []:
            if isinstance(v, dict) and BRIDGE_RE.match(k) and isinstance(v.get("token"), str) and v["token"].strip():
                self.accounts[k] = {"name": _text(v.get("name"), 60) or "Nuki", "token": v["token"].strip()[:200]}
        for k, v in (raw.get("brink") or {}).items() if isinstance(raw.get("brink"), dict) else []:
            if isinstance(v, dict) and BRIDGE_RE.match(k) and isinstance(v.get("email"), str) and v["email"].strip() and isinstance(v.get("password"), str) and v["password"]:
                self.brink_accounts[k] = {"name": _text(v.get("name"), 60) or "Brink Home", "email": v["email"].strip()[:200], "password": v["password"][:200]}
        for k, v in (raw.get("smappee") or {}).items() if isinstance(raw.get("smappee"), dict) else []:
            if isinstance(v, dict) and BRIDGE_RE.match(k) and isinstance(v.get("username"), str) and v["username"].strip() and isinstance(v.get("password"), str) and v["password"]:
                cid, sec = (_text(v.get("client_id"), 200), v["client_secret"][:200] if isinstance(v.get("client_secret"), str) else "")
                bud = v.get("budget")
                self.smappee_accounts[k] = {"name": _text(v.get("name"), 60) or "Smappee", "username": v["username"].strip()[:200], "password": v["password"][:200],
                                            "client_id": cid if cid and sec else "", "client_secret": sec if cid and sec else "",
                                            "budget": bud if isinstance(bud, int) and not isinstance(bud, bool) and 1 <= bud <= smappee.PUB_LIMIT else PUB_BUDGET}
        for k, v in (raw.get("devices") or {}).items():
            if isinstance(v, dict) and re.fullmatch(r"[a-z0-9_]{1,40}", k) and isinstance(v.get("hid"), str) and ID_RE.match(v["hid"]):
                self.devices[k] = self._defaults(v)
        if isinstance(raw.get("retired"), dict):
            self.retired = {t: float(ts) for t, ts in raw["retired"].items() if isinstance(t, str) and isinstance(ts, (int, float))}

    @staticmethod
    def _bridge_defaults(v: dict) -> dict | None:
        """Een bewaarde Hue-bridge, gecontroleerd. Zonder adres, sleutel of vingerafdruk is hij niet te gebruiken."""
        port = v.get("port")
        br = {"name": _text(v.get("name"), 60) or "Hue Bridge", "host": _text(v.get("host"), 253), "port": port if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 else hue.API_PORT,
              "key": _text(v.get("key"), 128), "fp": _text(v.get("fp"), 64).lower(), "bridge_id": _text(v.get("bridge_id"), 16).upper(),
              "own": v.get("own") is True}  # de sleutel is door deze add-on gemaakt (koppelen) en wordt bij loskoppelen ingetrokken; een sleutel die je zelf meegaf, is van iemand anders
        return br if HOST_RE.match(br["host"]) and br["key"] and hue.FP_RE.match(br["fp"]) and hue.BRIDGE_ID_RE.match(br["bridge_id"]) else None

    @staticmethod
    def _defaults(v: dict) -> dict:
        pts = []
        for r in v.get("points") if isinstance(v.get("points"), list) else []:
            p = _point(r)
            if p:
                if isinstance(r.get("gone"), (int, float)) and not isinstance(r.get("gone"), bool):
                    p["gone"] = float(r["gone"])
                pts.append(p)
        acts = clean_descriptor({"proto": "1.0", "rev": "x", "device": {"id": v["hid"]}, "actions": v.get("actions")})["actions"]
        ins = v.get("inputs") if isinstance(v.get("inputs"), dict) else {}
        shown = v.get("shown")
        port = v.get("port")
        driver = v["driver"] if v.get("driver") in ("esphome", "hue", "nuki", "brink", "smappee") else "huisdev"
        emap, skipped = v.get("emap"), v.get("skipped")
        scenes = v.get("scenes")
        rid = v.get("rid")
        return {"name": _text(v.get("name"), 60) or "Apparaat", "hid": v["hid"], "host": _text(v.get("host"), 253), "port": port if isinstance(port, int) and 1 <= port <= 65535 else 80,
                "type": _text(v.get("type"), 40), "fw": _text(v.get("fw"), 40), "proto": v.get("proto") if parse_proto(v.get("proto")) else "1.0", "rev": _text(v.get("rev"), 16),
                "token": _text(v.get("token"), 128), "points": pts, "actions": acts, "shown": [x for x in shown if isinstance(x, str)] if isinstance(shown, list) else None,
                "inputs": {k: r for k, r in ins.items() if isinstance(k, str) and isinstance(r, str)},
                "driver": driver,  # huisdev: het protocol van huisdev.h; esphome: de webserver van een ESPHome-toestel (esphome.py); hue: een apparaat van een Hue-bridge (hue.py)
                "bridge": v["bridge"] if driver == "hue" and isinstance(v.get("bridge"), str) and BRIDGE_RE.match(v["bridge"]) else "",  # hue: welke bridge
                "rid": rid if driver == "hue" and isinstance(rid, str) and hue.UUID_RE.match(rid) else "",  # hue: de UUID van het apparaat bij de bridge
                "account": v["account"] if driver in ("nuki", "brink", "smappee") and isinstance(v.get("account"), str) and BRIDGE_RE.match(v["account"]) else "",  # nuki: welk account (token)
                "lid": str(v["lid"]) if driver == "nuki" and LID_RE.match(str(v.get("lid") or "")) else "",  # nuki: het smartlockId
                "sid": str(v["sid"]) if (driver == "brink" and LID_RE.match(str(v.get("sid") or ""))) or (driver == "smappee" and SERIAL_RE.match(str(v.get("sid") or ""))) else "",  # brink: het systemShareId; smappee: het serienummer van het laadstation
                "cid": str(v["cid"]) if (driver == "brink" and LID_RE.match(str(v.get("cid") or ""))) or (driver == "smappee" and smappee.ID_RE.match(str(v.get("cid") or ""))) else "",  # brink: het componentId van de unit; smappee: het id van het smart device van het laadpunt
                "auth": _text(v.get("auth"), 200) if driver == "esphome" else "",  # gebruiker:wachtwoord van web_server: auth, alleen in dit bestand
                "emap": {k: x for k, x in emap.items() if isinstance(k, str) and isinstance(x, str)} if isinstance(emap, dict) else {},  # punt-id -> ESPHome-id, voor altijd vast
                "skipped": [x for x in skipped if isinstance(x, str)][:100] if isinstance(skipped, list) else [],
                # hue, een kamer: het vaste nummer van elke scène (id van de scène -> 1, 2, ...), zodat Loxone een scène als getal kan onthouden
                "scenes": {k: n for k, n in scenes.items() if isinstance(k, str) and hue.UUID_RE.match(k) and isinstance(n, int) and not isinstance(n, bool) and n > 0} if driver == "hue" and isinstance(scenes, dict) else {},
                # een ESPHome-toestel of Hue-lamp staat meestal al via zijn eigen integratie in HA: dubbele entiteiten alleen als de gebruiker dat wil
                "ha": v["ha"] if isinstance(v.get("ha"), bool) else driver == "huisdev"}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"devices": self.devices, "retired": self.retired, "bridges": self.bridges, "nuki": self.accounts, "brink": self.brink_accounts, "smappee": self.smappee_accounts}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ de punten van een apparaat

    @staticmethod
    def _by_id(dev: dict, pid: str) -> dict | None:
        return next((p for p in dev["points"] if p["id"] == pid), None)

    def point_type(self, ref: str) -> str | None:
        """Het type van de bron `e:<id>:<punt>` volgens de descriptor, of None (onbekend apparaat of punt)."""
        m = REF_RE.match(ref or "")
        dev = self.devices.get(m.group(1)) if m else None
        p = self._by_id(dev, m.group(2)) if dev else None
        if not p or p["kind"] == "text":
            return None
        return BOOL if p["kind"] == "bool" else NUMBER

    def _wirable(self, p: dict) -> bool:
        return p["kind"] != "text" and not p["diag"]

    def shown(self, dev: dict) -> list[str]:
        """Welke punten als rij op het blok staan: de keuze van de gebruiker, anders de eerste acht."""
        usable = [p["id"] for p in dev["points"] if self._wirable(p)]
        if dev["shown"] is None:
            return [i for i in usable if not self._by_id(dev, i).get("gone")][:DEFAULT_SHOWN]
        return [i for i in usable if i in dev["shown"]]

    def _retire(self, did: str, dev: dict, pid: str | None = None, action: str | None = None) -> None:
        """Zet de topics van een punt (alle domeinen waar het ooit in kon vallen) of een actie op de wislijst."""
        now = time.time()
        if action is not None:
            self.retired[f"{d.DISCOVERY}/button/{self._oid(did, 'do_' + action)}/config"] = now
            return
        for dom in DOMAINS:
            self.retired[f"{d.DISCOVERY}/{dom}/{self._oid(did, pid)}/config"] = now
        self.retired[self.t(did, pid, "state")] = now
        self.retired[self.t(did, pid, "avail")] = now

    def retire(self) -> list[str]:
        """Voor de MQTT-brug: topics van punten die weg zijn, ook uit een vorige run. Ze blijven 30 dagen op de lijst en worden bij elke volledige sync opnieuw gewist."""
        cutoff = time.time() - GONE_KEEP
        old = [t for t, ts in self.retired.items() if ts < cutoff]
        for t in old:
            del self.retired[t]
        if old:
            self._save()
        return list(self.retired)

    # ------------------------------------------------------------ toestellen

    def _check_name(self, name: str) -> str:
        name = (name or "").strip()[:60]
        if not name:
            raise ModelError("geef het apparaat een naam")
        return name

    def update(self, did: str, b: dict) -> None:
        dev = self.devices.get(did)
        if not dev:
            raise ModelError("onbekend apparaat")
        new = {**dev, "inputs": dict(dev["inputs"])}
        if "name" in b:
            new["name"] = self._check_name(b["name"])
        if "shown" in b:
            if not isinstance(b["shown"], list) or any(not isinstance(x, str) for x in b["shown"]):
                raise ModelError("shown: een lijst met punten")
            ok = {p["id"] for p in dev["points"] if self._wirable(p)}
            if any(x not in ok for x in b["shown"]):
                raise ModelError("onbekend punt")
            new["shown"] = [p["id"] for p in dev["points"] if p["id"] in b["shown"]]
        if "inputs" in b:
            new["inputs"] = self._inputs(did, dev, b["inputs"])
        if "host" in b and dev["driver"] == "hue":
            raise ModelError("het adres van een Hue-apparaat staat bij de bridge")
        if "host" in b and dev["driver"] == "smappee":
            raise ModelError("een Smappee-laadpunt heeft geen adres: het loopt via de cloud van Smappee")
        if "host" in b and dev["driver"] == "brink":
            raise ModelError("een Brink-unit heeft geen adres: het loopt via de cloud van Brink Home")
        if "host" in b and dev["driver"] == "nuki":
            raise ModelError("een Nuki-slot heeft geen adres: het loopt via de cloud van Nuki")
        if "host" in b:
            host = _text(b["host"], 253)
            if not HOST_RE.match(host):
                raise ModelError("ongeldig adres: geef het IP zonder http://")
            new["host"] = host
        if "ha" in b:
            if not isinstance(b["ha"], bool):
                raise ModelError("ha: aan of uit")
            new["ha"] = b["ha"]
        if new["inputs"] != dev["inputs"]:
            self.seen.pop(did, None)  # een andere bron: haar eerste stand wordt weer alleen onthouden
        self.devices[did] = new
        if dev["ha"] and not new["ha"]:  # niet meer naar HA: de entiteiten die er al staan moeten weg
            for p in dev["points"]:
                self._retire(did, dev, p["id"])
            for a in dev["actions"]:
                self._retire(did, dev, action=a["id"])
        self._save()
        if new["host"] != dev["host"]:
            self._kick(did)
        self._poke()

    def _inputs(self, did: str, dev: dict, b) -> dict:
        if not isinstance(b, dict):
            raise ModelError("ingangen: object verwacht")
        new = dict(dev["inputs"])
        m = self.engine.model
        for pid, ref in b.items():
            act = self._wired_action(dev, pid) if isinstance(pid, str) else None
            p = {"id": pid, "label": act["label"], "kind": "bool", "writable": True, "danger": False} if act else self._by_id(dev, pid) if isinstance(pid, str) else None
            if ref in (None, ""):
                new.pop(pid, None)
                continue
            if not p or not p["writable"] or p["danger"] or p["kind"] == "text":
                raise ModelError(f"{pid}: dit punt is niet te koppelen aan een bron")
            if not isinstance(ref, str):
                raise ModelError(f"ingang {p['label']}: ongeldige bron")
            got = source_type(ref, m.modules, m.timers)
            if got is None:
                raise ModelError(f"ingang {p['label']} wijst naar iets dat niet bestaat")
            want = BOOL if p["kind"] == "bool" else NUMBER
            if not fits(got, want):
                raise ModelError(f"ingang {p['label']} verwacht {'een 0 of 1' if want == BOOL else 'een getal'}" + (". Zet er een Groter dan of Kleiner dan tussen." if want == BOOL and got == NUMBER else ""))
            r = REF_RE.match(ref)
            if r and r.group(1) == did:
                raise ModelError("een apparaat stuurt zichzelf niet")
            new[pid] = ref
        return new

    async def claim(self, hid: str) -> str:
        """Koppel een gevonden apparaat: de descriptor ophalen, een token maken en het apparaat dat laten bewaren. Geeft het id van het blok."""
        f = self.found.get(hid)
        if not f:
            raise ModelError("dit apparaat is niet (meer) gevonden: zoek opnieuw")
        if any(x["hid"] == hid for x in self.devices.values()):
            raise ModelError("dit apparaat staat er al")
        if len(self.devices) >= MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} apparaten")
        if proto_state(f["proto"]) == "unsupported":
            raise ModelError(f"protocol {f['proto']} wordt niet ondersteund (de add-on kent {CURRENT[0]}.x): werk de add-on of het apparaat bij")
        tmp = {"host": f["ip"], "port": f["port"], "token": ""}
        try:
            desc = await self._fetch_descriptor(tmp)
            if desc["id"] != hid:
                raise ModelError(f"op {f['ip']} meldt zich nu {desc['id']}, niet {hid}: zoek opnieuw")
            if proto_state(desc["proto"]) == "unsupported":
                raise ModelError(f"protocol {desc['proto']} wordt niet ondersteund")
            token = secrets.token_hex(32)
            status, raw = await self._call(tmp, "POST", "/huisdev/claim", {"token": token}, token=False)
        except EspError as e:
            log.warning("koppelen van %s (%s:%s) mislukt: %s", hid, f["ip"], f["port"], e)
            raise ModelError(str(e)) from None
        if status == 409:
            raise ModelError(ERRORS["already_claimed"])
        if status != 200:
            raise ModelError(f"het apparaat weigert te koppelen ({(raw or {}).get('error', status)})")
        did = _slug(hid)
        dev = self._defaults({"hid": hid, "name": desc["name"] or f["name"] or hid, "host": f["ip"], "port": f["port"], "token": token})
        self.devices[did] = dev
        self._apply_desc(did, desc)
        self._save()
        self._start(did)
        self._poke()
        return did

    async def add_esphome(self, host: str, port=80, user: str = "", password: str = "", name: str = "") -> str:
        """Voeg een ESPHome-toestel toe via zijn webserver. De eerste eventstroom levert de entiteiten; er is niets te claimen en het toestel hoeft niet aangepast."""
        host = _text(host, 253)
        if not HOST_RE.match(host):
            raise ModelError("ongeldig adres: geef het IP (of de naam) van het toestel, zonder http://")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ModelError("ongeldige poort")
        if len(self.devices) >= MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} apparaten")
        if any(x["host"] == host and x["port"] == port for x in self.devices.values()):
            raise ModelError("dit apparaat staat er al")
        tmp = {"host": host, "port": port, "auth": f"{user}:{password}" if user else ""}
        st = self._es_new()
        rd = asyncio.ensure_future(self._es_read(tmp, st))
        try:
            end = time.monotonic() + ES_PROBE
            while time.monotonic() < end and not rd.done() and not self._es_ready(st):
                await asyncio.wait({rd}, timeout=0.05)
            if rd.done():
                rd.result()
        except EspError as e:
            raise ModelError(str(e)) from None
        finally:
            rd.cancel()
            await asyncio.gather(rd, return_exceptions=True)
        if not st["ent"]:
            raise ModelError(f"{host} meldt geen entiteiten: is dit een ESPHome-toestel met `web_server:` in de YAML?")
        title = st["title"] or host
        hid = ("esphome-" + esp.slug(title, "toestel"))[:32].rstrip("-")
        did = _slug(hid)
        if any(x["hid"] == hid for x in self.devices.values()):
            raise ModelError(f"er staat al een apparaat met de naam {title}")
        b = esp.build(st["ent"], {}, [])
        if not b["points"] and not b["actions"]:
            raise ModelError("dit toestel heeft niets wat we kennen (we ondersteunen sensor, schakelaar, licht, ventilator, keuze, getal, tekst en knop)")
        dev = self._defaults({"hid": hid, "name": _text(name, 60) or title, "host": host, "port": port, "driver": "esphome", "type": "esphome", "auth": tmp["auth"]})
        self.devices[did] = dev
        self._apply_desc(did, self._es_desc(dev, b))
        self._save()
        self._start(did)
        self._poke()
        return did

    async def remove(self, did: str, unclaim: bool = True) -> bool:
        dev = self.devices.get(did)
        if not dev:
            return False
        self._stop(did)
        if unclaim and dev["token"] and dev["driver"] == "huisdev":  # het apparaat meteen vrijgeven; lukt dat niet, dan wist u de claim op het apparaat zelf
            try:
                await self._call(dev, "POST", "/huisdev/unclaim", {})
            except EspError as e:
                log.warning("%s: claim intrekken mislukt (%s); wis hem op het apparaat", dev["name"], e)
        for p in dev["points"]:
            self._retire(did, dev, p["id"])
        for a in dev["actions"]:
            self._retire(did, dev, action=a["id"])
        del self.devices[did]
        for x in (self.values, self.off, self.seen, self.last, self.fails, self.errors, self.es, self.hue_desc, self.nuki_desc, self.brink_desc, self.smappee_desc):
            x.pop(did, None)
        self.online.discard(did)
        self._save()
        m = self.engine.model
        m.set_schema({**m.schema, "blocks": {k: v for k, v in m.schema["blocks"].items() if k != f"e:{did}"}})
        self._poke()
        return True

    # ------------------------------------------------------------ het netwerk

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
        return self._http

    async def _call(self, dev: dict, method: str, path: str, body=None, token: bool = True) -> tuple[int, dict]:
        status, raw = await self._raw(dev, method, path, body, token)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        return status, data if isinstance(data, dict) else {}

    async def _fetch_descriptor(self, dev: dict) -> dict:
        """`GET /huisdev/descriptor`, gecontroleerd. Bij een mislukking zegt de fout waarom (HTTP-status, geen JSON, of wat er in de beschrijving mist)."""
        status, raw = await self._raw(dev, "GET", "/huisdev/descriptor", None, False)
        if status != 200:
            raise EspError(status, "descriptor", f"het apparaat antwoordt op /huisdev/descriptor met HTTP {status}")
        try:
            j = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise EspError(status, "descriptor", f"het apparaat geeft geen leesbare JSON ({len(raw)} bytes, begint met {raw[:40]!r})") from None
        desc = clean_descriptor(j)
        if desc is None:
            raise EspError(status, "descriptor", f"het apparaat geeft geen geldige beschrijving (HUISDEV): {descriptor_problem(j)}")
        return desc

    async def _raw(self, dev: dict, method: str, path: str, body=None, token: bool = True) -> tuple[int, bytes]:
        url = f"http://{dev['host']}:{dev['port']}{path}"
        headers = {"X-Huisdev-Token": dev["token"]} if token and dev.get("token") else {}
        try:
            async with self._session().request(method, url, json=body, headers=headers) as r:
                # De hele body lezen: `content.read(n)` geeft maar terug wat er op dat moment binnen is, en een ESP stuurt zijn antwoord over
                # wifi in meerdere TCP-segmenten (een halve descriptor werd "geen geldige beschrijving").
                parts, size = [], 0
                async for chunk in r.content.iter_chunked(8192):
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise EspError(0, "net", "antwoord te groot")
                    parts.append(chunk)
                raw = b"".join(parts)
                status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise EspError(0, "net", f"geen verbinding met {dev['host']}: {e or 'time-out'}") from None
        return status, raw

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        for did in self.devices:
            self._start(did)
        for bid in self.bridges:
            self._start_bridge(bid)
        for aid in self.accounts:
            self._start_nuki(aid)
        for aid in self.brink_accounts:
            self._start_brink(aid)
        for aid in self.smappee_accounts:
            self._start_smappee(aid)
        self.tasks["_tick"] = loop.create_task(self._ticker())
        self.tasks["_scan"] = loop.create_task(self._scanner())

    async def _scanner(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port if self.port is not None else UDP_PORT))
            sock.setblocking(False)
            loop = asyncio.get_running_loop()
            self._listen, _ = await loop.create_datagram_endpoint(lambda: _Udp(self.on_datagram), sock=sock)
            self.udp_port = sock.getsockname()[1]
            q = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            q.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            q.bind(("0.0.0.0", 0))
            q.setblocking(False)
            self._ask, _ = await loop.create_datagram_endpoint(lambda: _Udp(self.on_datagram), sock=q)
        except OSError as e:
            log.warning("zoeken naar zelfbouwapparaten niet mogelijk: %s", e)
            return
        while True:
            if not self.devices or any(did not in self.online for did in self.devices) or not self.found:
                self.ask()
            await asyncio.sleep(SCAN_EVERY)

    def ask(self, target: str = "255.255.255.255", port: int | None = None) -> bool:
        if not self._ask:
            return False
        try:
            self._ask.sendto(f"HUISDEV?{CURRENT[0]}.{CURRENT[1]}".encode(), (target, port or self.udp_port))
        except OSError as e:
            log.warning("HUISDEV-vraag niet verstuurd: %s", e)
            return False
        return True

    def on_datagram(self, data: bytes, addr) -> None:
        f = parse_discovery(data, addr)
        if f:
            self.on_found(f)

    def on_found(self, f: dict) -> None:
        """Een apparaat antwoordt of kondigt zich aan. Een geclaimd apparaat volgt een nieuw IP (DHCP) als we het niet bereiken, en een nieuwe `rev` haalt meteen de descriptor."""
        new = f["hid"] not in self.found
        self.found[f["hid"]] = {**f, "seen": time.time()}
        if new:
            log.info("HUISDEV-apparaat %s (%s) gevonden op %s", f["hid"], f["type"] or "?", f["ip"])
        for did, dev in self.devices.items():
            if dev["hid"] != f["hid"]:
                continue
            if (dev["host"], dev["port"]) != (f["ip"], f["port"]) and did not in self.online:
                log.info("%s gevonden op %s", dev["name"], f["ip"])
                dev["host"], dev["port"] = f["ip"], f["port"]
                self._save()
            if f["rev"] and f["rev"] != dev["rev"]:
                self._kick(did)
            return
        if new:
            self._poke()

    def _kick(self, did: str) -> None:
        ev = self.wake.get(did)
        if ev:
            ev.set()

    def _start(self, did: str) -> None:
        if did in self.tasks or self.devices[did]["driver"] in ("hue", "nuki", "brink", "smappee"):  # een Hue-apparaat, Nuki-slot, Brink-unit of Smappee-laadpunt heeft geen eigen taak: zijn bridge of account volgt ze allemaal
            return
        self.wake[did] = asyncio.Event()
        run = self._run_esphome if self.devices[did]["driver"] == "esphome" else self._run
        self.tasks[did] = asyncio.get_running_loop().create_task(run(did))

    def _stop(self, did: str) -> None:
        t = self.tasks.pop(did, None)
        if t:
            t.cancel()
        self.wake.pop(did, None)

    def close(self) -> None:
        for k in list(self.tasks):
            self.tasks.pop(k).cancel()
        for s in (self._listen, self._ask):
            if s:
                s.close()
        self._listen = self._ask = None
        for attr in ("_http", "_stream_http"):
            http = getattr(self, attr)
            setattr(self, attr, None)
            if http and not http.closed:
                try:
                    asyncio.get_running_loop().create_task(http.close())
                except RuntimeError:  # geen lus meer: de sessie sluit zichzelf bij het afsluiten
                    pass
        if _model.esp_type == self.point_type:
            _model.esp_type = None

    async def _run(self, did: str) -> None:
        while True:
            dev = self.devices.get(did)
            if not dev:
                return
            try:
                status, data = await self._call(dev, "GET", "/huisdev/state")
                if status == 401:
                    raise EspError(401, "token")
                if status != 200:
                    raise EspError(status, "http", f"het apparaat antwoordt met {status}")
                await self._on_state(did, data)
            except EspError as e:
                self._miss(did, str(e), hard=e.status == 401)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - een rare state mag het volgen van dit apparaat niet voorgoed stilleggen
                log.exception("%s: state niet te verwerken", dev["name"])
                self._miss(did, "onbegrijpelijk antwoord")
            wait = self.poll if did in self.online else max(self.poll, min(self.poll * 3, 10.0))
            ev = self.wake.get(did)
            try:
                await asyncio.wait_for(ev.wait(), wait) if ev else await asyncio.sleep(wait)
            except asyncio.TimeoutError:
                pass
            if ev:
                ev.clear()

    def _miss(self, did: str, why: str, hard: bool = False) -> None:
        self.errors[did] = why
        self.fails[did] = self.fails.get(did, 0) + 1
        if (hard or self.fails[did] >= FAILS) and did in self.online:
            self.online.discard(did)
            self._poke()
        elif hard or self.fails[did] == FAILS:
            self._poke()

    async def _on_state(self, did: str, data: dict) -> None:
        dev = self.devices[did]
        if proto_state(data.get("proto")) == "unsupported":
            raise EspError(0, "proto", f"protocol {data.get('proto')} wordt niet ondersteund")
        rev = data.get("rev")
        if isinstance(rev, str) and rev != dev["rev"]:
            await self._refetch(did)
            dev = self.devices[did]
        known = {p["id"]: p for p in dev["points"]}
        vals: dict[str, dict] = {}
        now = time.monotonic()
        for pid, e in (data.get("points") if isinstance(data.get("points"), dict) else {}).items():
            p = known.get(pid)
            if not p or not isinstance(e, dict):
                continue
            v = self._value(p, e.get("v"))
            age = e.get("age")
            vals[pid] = {"v": v, "age": age if isinstance(age, (int, float)) and not isinstance(age, bool) and age >= 0 else 0, "at": now}
        offs = {a: str((e or {}).get("reason") or "")[:100] for a, e in (data.get("actions") if isinstance(data.get("actions"), dict) else {}).items()
                if isinstance(e, dict) and e.get("disabled") is True}
        changed = did not in self.online or {k: v["v"] for k, v in self.values.get(did, {}).items()} != {k: v["v"] for k, v in vals.items()} or self.off.get(did) != offs
        self.values[did], self.off[did] = vals, offs
        self.fails[did] = 0
        self.errors[did] = ""
        self.online.add(did)
        if changed:
            self._poke()

    @staticmethod
    def _value(p: dict, v):
        """Wat het apparaat meldt, als de waarde van zijn type. Alles wat niet past is onbekend (None), nooit een gok."""
        if v is None:
            return None
        k = p["kind"]
        if k == "bool":
            return v if isinstance(v, bool) else None
        if k == "text":
            return v[:255] if isinstance(v, str) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            return None
        if k in ("int", "enum"):
            return int(round(v)) if abs(v - round(v)) < 1e-9 else None
        return float(v)

    # ------------------------------------------------------------ ESPHome: een eventstroom in plaats van een poll

    def _es_new(self) -> dict:
        return {"ent": {}, "title": "", "pinged": False, "dirty": False, "desc_dirty": True, "desc": None, "t0": time.monotonic()}

    @staticmethod
    def _es_ready(st: dict) -> bool:
        """Er is iets nieuws en de eerste stroom volledige regels is binnen (anders groeit de descriptor in stukjes en springt `shown` rond)."""
        return st["dirty"] and time.monotonic() - st["t0"] >= ES_SETTLE and bool(st["ent"] or st["pinged"])

    def _es_session(self) -> aiohttp.ClientSession:
        if self._stream_http is None or self._stream_http.closed:
            self._stream_http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=HTTP_TIMEOUT, sock_read=ES_READ))
        return self._stream_http

    @staticmethod
    def _es_headers(dev: dict, **extra) -> dict:
        """Basic-auth als `web_server: auth` aan staat. Zelf gebouwd: `aiohttp.BasicAuth` en `auth=` zijn in nieuwere versies verouderd."""
        user, sep, pw = (dev.get("auth") or "").partition(":")
        if sep and user:
            extra["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        return extra

    async def _es_read(self, dev: dict, st: dict) -> None:
        """Eén verbinding met `GET /events`: de volledige regels (met naam, eenheid, keuzes) en de wijzigingen komen in `st`. Eindigt altijd met een EspError."""
        url = f"http://{dev['host']}:{dev['port']}/events"
        sse = esp.Sse()
        st.update(ent={}, pinged=False, dirty=True, desc_dirty=True, t0=time.monotonic())
        try:
            async with self._es_session().get(url, headers=self._es_headers(dev, Accept="text/event-stream")) as r:
                if r.status == 401:
                    raise EspError(401, "auth", "het toestel vraagt een gebruikersnaam en wachtwoord (web_server: auth)")
                if r.status != 200:
                    raise EspError(r.status, "http", f"{dev['host']} antwoordt op /events met HTTP {r.status}: staat `web_server:` aan in de YAML?")
                async for chunk in r.content.iter_any():
                    try:
                        events = sse.feed(chunk)
                    except ValueError:
                        raise EspError(0, "net", f"{dev['host']} geeft geen ESPHome-eventstroom") from None
                    for ev, data in events:
                        got = esp.parse_event(ev, data)
                        if not got:
                            continue
                        kind, j = got
                        if kind == "ping":
                            st["pinged"] = True
                            if j:
                                st["title"] = j
                            continue
                        ent = st["ent"].setdefault(j["id"], {})
                        ent.update(j)
                        st["dirty"] = True
                        if "name" in j:
                            st["desc_dirty"] = True
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise EspError(0, "net", f"geen verbinding met {dev['host']}: {e or 'time-out'}") from None
        raise EspError(0, "net", "het toestel sloot de verbinding")

    def _es_desc(self, dev: dict, b: dict) -> dict:
        return {"proto": "1.0", "rev": b["rev"], "id": dev["hid"], "type": "esphome", "name": dev["name"], "fw": dev["fw"], "lib": "", "points": b["points"], "actions": b["actions"],
                "emap": b["emap"], "skipped": b["skipped"]}

    async def _es_flush(self, did: str) -> None:
        """De stand van de eventstroom als een HUISDEV-state verwerken. Een andere set entiteiten is een andere `rev`: `_on_state` past dan het blok aan."""
        st, dev = self.es[did], self.devices[did]
        if st["desc_dirty"] or st["desc"] is None:
            st["desc"] = self._es_desc(dev, esp.build(st["ent"], dev["emap"], dev["points"]))
            st["desc_dirty"] = False
        desc = st["desc"]
        pts = {}
        for p in desc["points"]:
            v = esp.value_of(p, st["ent"].get(desc["emap"].get(p["id"], "")))
            pts[p["id"]] = {"v": v, "age": 0} if v is not None else {"v": None}
        st["dirty"] = False
        await self._on_state(did, {"proto": "1.0", "rev": desc["rev"], "points": pts, "actions": {}})

    async def _run_esphome(self, did: str) -> None:
        st = self.es.setdefault(did, self._es_new())
        while True:
            dev = self.devices.get(did)
            if not dev:
                return
            rd = asyncio.ensure_future(self._es_read(dev, st))
            try:
                while not rd.done():
                    await asyncio.wait({rd}, timeout=ES_FLUSH)
                    if not rd.done() and self._es_ready(st):
                        await self._es_flush(did)
                rd.result()
            except EspError as e:
                self._miss(did, str(e), hard=True)  # een stroom die weg is, laat geen waarde meer vers: meteen offline
            except asyncio.CancelledError:
                rd.cancel()
                raise
            except Exception:  # noqa: BLE001 - een rare regel mag het volgen van dit toestel niet voorgoed stilleggen
                log.exception("%s: eventstroom niet te verwerken", dev["name"])
                self._miss(did, "onbegrijpelijk antwoord", hard=True)
            finally:
                rd.cancel()
            wait = max(self.poll, min(self.poll * 3, 10.0))
            ev = self.wake.get(did)
            try:
                await asyncio.wait_for(ev.wait(), wait) if ev else await asyncio.sleep(wait)
            except asyncio.TimeoutError:
                pass
            if ev:
                ev.clear()

    async def _es_send(self, did: str, path: str, params: dict) -> dict:
        dev = self.devices.get(did)
        if not dev:
            raise ModelError("onbekend apparaat")
        if did not in self.online:
            raise ModelError("niet verstuurd: geen verbinding met het apparaat")
        url = f"http://{dev['host']}:{dev['port']}{path}"
        try:
            async with self._session().post(url, params=params, headers=self._es_headers(dev)) as r:
                await r.read()
                status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise ModelError(f"geen verbinding met {dev['host']}: {e or 'time-out'}") from None
        if status == 404:
            raise ModelError("het toestel kent dit niet (meer)")
        if status == 401:
            raise ModelError("het toestel weigert de gebruikersnaam of het wachtwoord")
        if status != 200:
            raise ModelError(f"het toestel antwoordt met HTTP {status}")
        return {"ok": True, "result": "applied"}  # de nieuwe stand komt vanzelf over de eventstroom

    # ------------------------------------------------------------ Hue: een bridge met een eventstroom, en een blok per apparaat

    async def add_hue_bridge(self, host: str, port=hue.API_PORT, key: str = "", name: str = "") -> str:
        """Een Hue-bridge koppelen. Zonder `key` moet de ronde knop op de bridge net zijn ingedrukt (30 s); met `key` (een bestaande applicatiesleutel) niet.
        Het certificaat dat de bridge nu toont wordt vastgezet: het is van Signify en door geen publieke autoriteit getekend. Geeft het id van de bridge."""
        host = _text(host, 253)
        if not HOST_RE.match(host):
            raise ModelError("ongeldig adres: geef het IP van de bridge, zonder https://")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ModelError("ongeldige poort")
        if len(self.bridges) >= MAX_BRIDGES:
            raise ModelError(f"maximaal {MAX_BRIDGES} bridges")
        if any(b["host"] == host and b["port"] == port for b in self.bridges.values()):
            raise ModelError("deze bridge staat er al")
        key = _text(key, 128)
        try:
            fp = await hue.fetch_fingerprint(host, port)
            cfg = await hue.config(self._session(), host, port, fp)
            bridge_id = str(cfg["bridgeid"]).upper()
            if any(b["bridge_id"] == bridge_id for b in self.bridges.values()):
                raise ModelError("deze bridge staat er al (onder een ander adres)")
            br = {"name": _text(name, 60) or _text(cfg.get("name"), 60) or "Hue Bridge", "host": host, "port": port, "key": key, "fp": fp, "bridge_id": bridge_id, "own": not key}
            if not key:
                br["key"] = await hue.pair(self._session(), host, port, fp)
            else:
                status, data = await hue.request(self._session(), br, "GET", f"{hue.RESOURCE}/bridge")
                if status != 200 or not isinstance(data, dict) or not data.get("data"):
                    raise ModelError("de bridge accepteert deze sleutel niet")
        except hue.HueError as e:
            log.warning("Hue-bridge %s koppelen mislukt: %s", host, e)
            raise ModelError(e.reason) from None
        bid = "b_" + bridge_id[-6:].lower()
        self.bridges[bid] = br
        self._save()
        self._start_bridge(bid)
        self._poke()
        return bid

    async def verify_hue_bridge(self, bid: str, host: str | None = None, name: str | None = None) -> None:
        """Het adres of het certificaat van een bridge opnieuw vastleggen, nadat is vastgesteld dat het dezelfde bridge is (zijn `bridgeid`).
        Een bridge die een nieuw adres krijgt (DHCP) of na een reset een nieuw certificaat, gaat zo weer aan zonder opnieuw te koppelen."""
        br = self.bridges.get(bid)
        if not br:
            raise ModelError("onbekende bridge")
        if name is not None:
            br = {**br, "name": _text(name, 60) or br["name"]}
        host = _text(host, 253) if host is not None else br["host"]
        if not HOST_RE.match(host):
            raise ModelError("ongeldig adres: geef het IP van de bridge, zonder https://")
        try:
            fp = await hue.fetch_fingerprint(host, br["port"])
            cfg = await hue.config(self._session(), host, br["port"], fp)
        except hue.HueError as e:
            raise ModelError(e.reason) from None
        if str(cfg["bridgeid"]).upper() != br["bridge_id"]:
            raise ModelError(f"op {host} zit een andere bridge ({cfg['bridgeid']}), niet {br['bridge_id']}")
        self.bridges[bid] = {**br, "host": host, "fp": fp}
        self._save()
        self._hue_kick(bid)
        self._poke()

    async def remove_hue_bridge(self, bid: str) -> bool:
        """De bridge en al zijn blokken weghalen. Een sleutel die de add-on bij het koppelen maakte, wordt, als het kan, ook op de bridge ingetrokken (v1-API)."""
        br = self.bridges.get(bid)
        if not br:
            return False
        for did in [k for k, v in self.devices.items() if v["driver"] == "hue" and v["bridge"] == bid]:
            await self.remove(did)
        t = self.tasks.pop(f"hue:{bid}", None)
        if t:
            t.cancel()
        self.wake.pop(f"hue:{bid}", None)
        if br["own"]:  # een sleutel die je zelf meegaf, kan ook ergens anders in gebruik zijn (een ander programma): die blijft
            try:
                await hue.request(self._session(), br, "DELETE", f"/api/{br['key']}/config/whitelist/{br['key']}", key=False)
            except hue.HueError as e:
                log.warning("Hue-bridge %s: de sleutel intrekken mislukte (%s); haal hem weg in de Hue-app", br["host"], e)
        del self.bridges[bid]
        for x in (self.hue, self._hue_locks, self._hue_sent):
            x.pop(bid, None)
        self._save()
        self._poke()
        return True

    async def claim_hue(self, bid: str, rids: list[str] | None = None) -> list[str]:
        """Apparaten van een bridge als blok toevoegen (`rids` leeg: alles wat er nog niet staat). Geeft de ids van de nieuwe apparaten, in de volgorde van de lijst."""
        st = self.hue.get(bid)
        if bid not in self.bridges or not st or not st["store"]:
            raise ModelError("de bridge is niet bereikbaar: er is nog geen lijst van apparaten")
        offered = hue.devices(st["store"])
        have = {v["rid"] for v in self.devices.values() if v["driver"] == "hue" and v["bridge"] == bid}
        if rids:
            unknown = [r for r in rids if r not in {o["rid"] for o in offered}]
            if unknown:
                raise ModelError("de bridge kent dit apparaat niet (meer)")
            take = [o for o in offered if o["rid"] in rids and o["rid"] not in have]
        else:
            take = [o for o in offered if o["rid"] not in have]
        if not take:
            raise ModelError("alles wat de bridge aanbiedt staat er al")
        if len(self.devices) + len(take) > MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} apparaten")
        new = []
        for o in take:
            nums: dict = {}
            hue.assign_scenes(nums, st["store"][o["rid"]], st["store"])  # alleen een kamer heeft scènes
            desc = hue.describe(st["store"][o["rid"]], st["store"], numbers=nums)
            did = _slug(o["hid"])
            if did in self.devices:
                continue
            self.devices[did] = self._defaults({"hid": o["hid"], "name": o["name"], "driver": "hue", "bridge": bid, "rid": o["rid"], "type": desc["type"], "fw": desc["fw"], "scenes": nums})
            self.hue_desc[did] = desc
            self._apply_desc(did, desc)
            new.append(did)
        self._save()
        if st["online"]:
            await self._hue_flush(bid)
        self._poke()
        return new

    def _start_bridge(self, bid: str) -> None:
        if f"hue:{bid}" not in self.tasks:
            self.hue.setdefault(bid, self._hue_new())
            self.wake[f"hue:{bid}"] = asyncio.Event()
            self.tasks[f"hue:{bid}"] = asyncio.get_running_loop().create_task(self._run_hue(bid))

    def _hue_kick(self, bid: str) -> None:
        self._kick(f"hue:{bid}")

    def _hue_children(self, bid: str) -> list[str]:
        return [did for did, dev in self.devices.items() if dev["driver"] == "hue" and dev["bridge"] == bid]

    @staticmethod
    def _hue_new() -> dict:
        return {"store": {}, "open": False, "online": False, "need_sync": True, "syncing": False, "pending": [], "dirty": False, "synced": 0.0, "alive": 0.0, "error": "", "cert": False}

    async def _hue_read(self, bid: str, st: dict) -> None:
        """Eén verbinding met de eventstroom van de bridge: wijzigingen komen in `st['store']`. Eindigt altijd met een HueError."""
        br = self.bridges[bid]
        url = f"https://{br['host']}:{br['port']}{hue.STREAM}"
        sse = esp.Sse()
        st.update(open=False, need_sync=True)
        try:
            async with self._es_session().get(url, headers={"hue-application-key": br["key"], "Accept": "text/event-stream"}, ssl=hue.ssl_for(br["fp"]),
                                              timeout=aiohttp.ClientTimeout(total=None, sock_connect=hue.TIMEOUT, sock_read=None)) as r:  # geen leestime-out: de stroom zwijgt als er niets verandert
                if r.status in (401, 403):
                    raise hue.HueError("key", "de bridge weigert de sleutel: koppel de bridge opnieuw", r.status)
                if r.status != 200:
                    raise hue.HueError("net", f"{br['host']} antwoordt op de eventstroom met HTTP {r.status}", r.status)
                st["open"] = True
                async for chunk in r.content.iter_any():
                    try:
                        got = sse.feed(chunk)
                    except ValueError:
                        raise hue.HueError("net", f"{br['host']} geeft geen eventstroom van een Hue-bridge") from None
                    for _ev, data in got:
                        for kind, res in hue.events(data):
                            if st["syncing"]:
                                st["pending"].append((kind, res))  # de lijst wordt net opgehaald: dit hoort er na
                            else:
                                hue.apply(st["store"], kind, res)
                                st["dirty"] = True
        except aiohttp.ServerFingerprintMismatch:
            raise hue.HueError("cert", f"het certificaat van de bridge op {br['host']} is veranderd (bridge vervangen of teruggezet?): vertrouw hem opnieuw") from None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise hue.HueError("net", f"geen verbinding met {br['host']}: {e or 'time-out'}") from None
        raise hue.HueError("net", "de bridge sloot de eventstroom")

    async def _hue_sync(self, bid: str, st: dict) -> None:
        """De hele lijst ophalen. Wat de eventstroom tijdens het ophalen bracht, komt er daarna overheen."""
        st["syncing"], st["pending"] = True, []
        try:
            status, data = await hue.request(self._session(), self.bridges[bid], "GET", hue.RESOURCE)
            if status != 200 or not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise hue.HueError("net", f"de bridge antwoordt met HTTP {status}", status)
            st["store"] = hue.index(data["data"])
            for kind, res in st["pending"]:
                hue.apply(st["store"], kind, res)
        finally:
            st["syncing"], st["pending"] = False, []
        st.update(need_sync=False, dirty=True, synced=time.monotonic(), alive=time.monotonic())

    async def _hue_ping(self, bid: str, st: dict) -> None:
        """Leeft de bridge nog? De eventstroom zegt dat niet: bij een stroomuitval of een weggevallen kabel komt er geen sluiting."""
        status, _ = await hue.request(self._session(), self.bridges[bid], "GET", f"{hue.RESOURCE}/bridge")
        if status != 200:
            raise hue.HueError("net", f"de bridge antwoordt met HTTP {status}", status)
        st["alive"] = time.monotonic()

    async def _hue_flush(self, bid: str) -> None:
        """De lijst van de bridge als HUISDEV-state naar elk apparaat. Een andere set diensten is een andere `rev`: `_on_state` past het blok dan aan."""
        st = self.hue[bid]
        st.update(dirty=False, online=True, error="", cert=False)
        store = st["store"]
        for did in self._hue_children(bid):
            dev = self.devices[did]
            d = store.get(dev["rid"])
            if d and hue.assign_scenes(dev["scenes"], d, store):
                self._save()  # een nieuwe scène krijgt een nummer dat blijft
            desc = hue.describe(d, store, dev["name"], dev["scenes"]) if d else None
            if desc is None:
                self._miss(did, "dit apparaat bestaat niet meer op de bridge (of is geen lamp, stopcontact, melder of kamer meer)", hard=True)
                continue
            self.hue_desc[did] = desc
            vals = hue.values(d, store, dev["scenes"])
            pts = {p["id"]: ({"v": vals[p["id"]], "age": 0} if vals.get(p["id"]) is not None else {"v": None}) for p in desc["points"]}
            await self._on_state(did, {"proto": "1.0", "rev": desc["rev"], "points": pts, "actions": {}})
        self._poke()

    def _hue_down(self, bid: str, st: dict, e: hue.HueError) -> None:
        st.update(open=False, online=False, error=str(e), cert=e.code == "cert", need_sync=True)
        for did in self._hue_children(bid):
            self._miss(did, str(e), hard=True)  # een bridge die we niet meer horen, laat geen waarde meer vers: meteen offline
        self._poke()

    async def _run_hue(self, bid: str) -> None:
        while True:
            if bid not in self.bridges:
                return
            st = self.hue.setdefault(bid, self._hue_new())
            rd = asyncio.ensure_future(self._hue_read(bid, st))
            try:
                while not rd.done():
                    await asyncio.wait({rd}, timeout=HUE_FLUSH)
                    if rd.done() or not st["open"]:
                        continue
                    now = time.monotonic()
                    if st["need_sync"] or now - st["synced"] > HUE_RESYNC:
                        await self._hue_sync(bid, st)
                    elif now - st["alive"] > HUE_PING:
                        await self._hue_ping(bid, st)
                    if st["dirty"] and not st["need_sync"]:
                        await self._hue_flush(bid)
                rd.result()
            except hue.HueError as e:
                self._hue_down(bid, st, e)
            except asyncio.CancelledError:
                rd.cancel()
                raise
            except Exception:  # noqa: BLE001 - een rare gebeurtenis mag het volgen van deze bridge niet voorgoed stilleggen
                log.exception("Hue-bridge %s: eventstroom niet te verwerken", bid)
                self._hue_down(bid, st, hue.HueError("net", "onbegrijpelijk antwoord"))
            finally:
                rd.cancel()
            wait = max(self.poll, min(self.poll * 3, 10.0))
            ev = self.wake.get(f"hue:{bid}")
            try:
                await asyncio.wait_for(ev.wait(), wait) if ev else await asyncio.sleep(wait)
            except asyncio.TimeoutError:
                pass
            if ev:
                ev.clear()

    async def _hue_send(self, did: str, dev: dict, pid: str, v) -> dict:
        bid = dev["bridge"]
        br, st = self.bridges.get(bid), self.hue.get(bid)
        if not br or not st or did not in self.online:
            raise ModelError("niet verstuurd: geen verbinding met de bridge")
        d = st["store"].get(dev["rid"])
        if not d:
            raise ModelError("dit apparaat bestaat niet meer op de bridge")
        try:
            path, body = hue.point_request(d, st["store"], pid, v, dev.get("scenes"))
        except ValueError as e:
            raise ModelError(str(e)) from None
        async with self._hue_locks.setdefault(bid, asyncio.Lock()):  # een bridge verdraagt maar ~10 opdrachten per seconde
            wait = HUE_GAP - (time.monotonic() - self._hue_sent.get(bid, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                status, data = await hue.request(self._session(), br, "PUT", path, body)
            except hue.HueError as e:
                if e.code == "key":  # de eventstroom blijft open na een ingetrokken sleutel: laat de lijst opnieuw ophalen, die weigert dan en zet alles offline
                    st["need_sync"] = True
                raise ModelError(e.reason) from None
            finally:
                self._hue_sent[bid] = time.monotonic()
        why = hue.problems(data)
        if status != 200 or why:
            raise ModelError(f"de bridge weigert dit: {why or f'HTTP {status}'}")
        return {"ok": True, "result": "applied"}  # de nieuwe stand komt vanzelf over de eventstroom

    def hue_view(self) -> list[dict]:
        """De bridges voor de pagina, met wat ze aanbieden. Nooit de sleutel of de vingerafdruk."""
        out = []
        for bid, br in self.bridges.items():
            st = self.hue.get(bid) or self._hue_new()
            have = {v["rid"]: k for k, v in self.devices.items() if v["driver"] == "hue" and v["bridge"] == bid}
            out.append({"id": bid, "name": br["name"], "host": br["host"], "port": br["port"], "bridge_id": br["bridge_id"], "online": st["online"], "error": st["error"], "cert": st["cert"],
                        "devices": [{**o, "id": have.get(o["rid"]), "claimed": o["rid"] in have} for o in hue.devices(st["store"])], "skipped": hue.skipped(st["store"])})
        return out

    # ------------------------------------------------------------ Nuki: een account (token) dat gepold wordt, en een blok per slot

    async def add_nuki(self, token: str, name: str = "") -> str:
        """Een Nuki-account koppelen met een API-token (web.nuki.io, Integraties). De token wordt meteen gecontroleerd door de sloten op te halen. Geeft het id van het account."""
        token = _text(token, 200)
        if not re.fullmatch(r"\S{8,200}", token):
            raise ModelError("geef de API-token van Nuki (web.nuki.io, Integraties)")
        aid = nuki.account_id(token)
        if aid in self.accounts:
            raise ModelError("deze token staat er al")
        if len(self.accounts) >= MAX_ACCOUNTS:
            raise ModelError(f"maximaal {MAX_ACCOUNTS} Nuki-accounts")
        try:
            status, data = await nuki.request(self._session(), token, "GET", "/smartlock")
        except nuki.NukiError as e:
            log.warning("Nuki koppelen mislukt: %s", e)
            raise ModelError(e.reason) from None
        if status != 200:
            raise ModelError(f"Nuki antwoordt met HTTP {status}: {nuki.problem(data) or 'onbekende fout'}")
        found = nuki.locks(data)
        if not found:
            raise ModelError("deze token geeft toegang tot geen enkel slot")
        self.accounts[aid] = {"name": _text(name, 60) or "Nuki", "token": token}
        st = self.nk[aid] = self._nuki_new()
        st.update(locks=found, online=True)  # de eerste lijst hebben we al: het menu hoeft niet op de eerste poll te wachten
        self._save()
        self._start_nuki(aid)
        self._poke()
        return aid

    async def remove_nuki(self, aid: str) -> bool:
        """Het account en al zijn blokken weghalen. De token blijft geldig bij Nuki: trek hem zelf in op web.nuki.io als je hem niet meer wilt."""
        if aid not in self.accounts:
            return False
        for did in [k for k, v in self.devices.items() if v["driver"] == "nuki" and v["account"] == aid]:
            await self.remove(did)
        t = self.tasks.pop(f"nuki:{aid}", None)
        if t:
            t.cancel()
        self.wake.pop(f"nuki:{aid}", None)
        del self.accounts[aid]
        self.nk.pop(aid, None)
        self._save()
        self._poke()
        return True

    async def claim_nuki(self, aid: str, lids: list[str] | None = None) -> list[str]:
        """Sloten van een account als blok toevoegen (`lids` leeg: alles wat er nog niet staat). Geeft de ids van de nieuwe apparaten."""
        st = self.nk.get(aid)
        if aid not in self.accounts or not st or not st["locks"]:
            raise ModelError("Nuki is niet bereikbaar: er is nog geen lijst van sloten")
        have = {v["lid"] for v in self.devices.values() if v["driver"] == "nuki" and v["account"] == aid}
        if lids:
            if any(x not in st["locks"] for x in lids):
                raise ModelError("Nuki kent dit slot niet (meer)")
            take = [x for x in lids if x not in have]
        else:
            take = [x for x in st["locks"] if x not in have]
        if not take:
            raise ModelError("alles wat Nuki aanbiedt staat er al")
        if len(self.devices) + len(take) > MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} apparaten")
        new = []
        for lid in take:
            desc = nuki.describe(st["locks"][lid])
            did = _slug(nuki.hid_of(lid))
            if did in self.devices:
                continue
            self.devices[did] = self._defaults({"hid": nuki.hid_of(lid), "name": desc["name"], "driver": "nuki", "account": aid, "lid": lid, "type": desc["type"]})
            self.nuki_desc[did] = desc
            self._apply_desc(did, desc)
            new.append(did)
        self._save()
        if st["online"]:
            await self._nuki_flush(aid)
        self._poke()
        return new

    def _start_nuki(self, aid: str) -> None:
        if f"nuki:{aid}" not in self.tasks:
            self.nk.setdefault(aid, self._nuki_new())
            self.wake[f"nuki:{aid}"] = asyncio.Event()
            self.tasks[f"nuki:{aid}"] = asyncio.get_running_loop().create_task(self._run_nuki(aid))

    @staticmethod
    def _nuki_new() -> dict:
        return {"locks": {}, "online": False, "error": "", "key": False, "burst": []}

    def _nuki_kick(self, aid: str) -> None:
        """Na een opdracht: Nuki voert hem asynchroon uit, dus over 2, 6 en 15 s kijken we of het slot er is."""
        st = self.nk.get(aid)
        if st is not None:
            st["burst"] = list(NUKI_BURST)
        self._kick(f"nuki:{aid}")

    def _nuki_children(self, aid: str) -> list[str]:
        return [did for did, dev in self.devices.items() if dev["driver"] == "nuki" and dev["account"] == aid]

    async def _nuki_flush(self, aid: str) -> None:
        """De lijst van Nuki als HUISDEV-state naar elk slot. Een deursensor die erbij komt is een andere `rev`: `_on_state` past het blok dan aan."""
        st = self.nk[aid]
        st.update(online=True, error="", key=False)
        for did in self._nuki_children(aid):
            dev = self.devices[did]
            lock = st["locks"].get(dev["lid"])
            if lock is None:
                self._miss(did, "dit slot staat niet meer in je Nuki-account", hard=True)
                continue
            desc = nuki.describe(lock, dev["name"])
            self.nuki_desc[did] = desc
            vals = nuki.values(lock)
            pts = {p["id"]: ({"v": vals[p["id"]], "age": 0} if vals.get(p["id"]) is not None else {"v": None}) for p in desc["points"]}
            await self._on_state(did, {"proto": "1.0", "rev": desc["rev"], "points": pts, "actions": {}})
        self._poke()

    def _nuki_down(self, aid: str, st: dict, e: nuki.NukiError) -> None:
        st.update(online=False, error=str(e), key=e.code == "key")
        for did in self._nuki_children(aid):
            self._miss(did, str(e), hard=True)  # wat we niet meer horen, laat geen waarde meer vers: meteen offline
        self._poke()

    async def _run_nuki(self, aid: str) -> None:
        while True:
            acc = self.accounts.get(aid)
            if not acc:
                return
            st = self.nk.setdefault(aid, self._nuki_new())
            backoff = False
            try:
                status, data = await nuki.request(self._session(), acc["token"], "GET", "/smartlock")
                if status != 200:
                    raise nuki.NukiError("net", f"Nuki antwoordt met HTTP {status}", status)
                st["locks"] = nuki.locks(data)
                await self._nuki_flush(aid)
            except nuki.NukiError as e:
                self._nuki_down(aid, st, e)
                backoff = e.code == "rate"
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - een rare lijst mag het volgen van dit account niet voorgoed stilleggen
                log.exception("Nuki %s: lijst niet te verwerken", aid)
                self._nuki_down(aid, st, nuki.NukiError("net", "onbegrijpelijk antwoord"))
            delay = NUKI_BACKOFF if backoff else st["burst"].pop(0) if st["burst"] else NUKI_POLL
            ev = self.wake.get(f"nuki:{aid}")
            woke = False
            try:
                await asyncio.wait_for(ev.wait(), delay) if ev else await asyncio.sleep(delay)
                woke = ev is not None
            except asyncio.TimeoutError:
                pass
            if ev:
                ev.clear()
            if woke and st["burst"] and not backoff:  # een opdracht net verstuurd: eerst de tijd geven die het slot nodig heeft
                await asyncio.sleep(st["burst"].pop(0))

    async def _nuki_send(self, did: str, dev: dict, action: str) -> dict:
        acc = self.accounts.get(dev["account"])
        if not acc or did not in self.online:
            raise ModelError("niet verstuurd: geen verbinding met Nuki")
        try:
            path, body = nuki.action_request(dev["lid"], action)
        except ValueError as e:
            raise ModelError(str(e)) from None
        try:
            status, data = await nuki.request(self._session(), acc["token"], "POST", path, body)
        except nuki.NukiError as e:
            raise ModelError(e.reason) from None
        if status not in (200, 204):
            why = nuki.problem(data)
            hint = " (dit slot of deze regio ondersteunt unlatch niet: gebruik ontgrendelen)" if status == 400 and action.startswith("unlatch") else ""
            raise ModelError(f"Nuki weigert dit: {why or f'HTTP {status}'}{hint}")
        self._nuki_kick(dev["account"])
        return {"ok": True, "result": "queued"}  # Nuki nam de opdracht aan; of het slot hem uitvoerde, zie je aan de stand bij de volgende poll

    def nuki_view(self) -> list[dict]:
        """De Nuki-accounts voor de pagina, met wat ze aanbieden. Nooit de token."""
        out = []
        for aid, acc in self.accounts.items():
            st = self.nk.get(aid) or self._nuki_new()
            have = {v["lid"]: k for k, v in self.devices.items() if v["driver"] == "nuki" and v["account"] == aid}
            out.append({"id": aid, "name": acc["name"], "online": st["online"], "error": st["error"], "key": st["key"],
                        "locks": [{**o, "id": have.get(o["lid"]), "claimed": o["lid"] in have} for o in nuki.listing(st["locks"])]})
        return out

    # ------------------------------------------------------------ Brink Home: een account (e-mail en wachtwoord) dat gepold wordt, en een blok per ventilatie-unit

    async def add_brink(self, email: str, password: str, name: str = "") -> str:
        """Een Brink Home-account koppelen. Het wachtwoord wordt meteen gecontroleerd door in te loggen. Geeft het id van het account."""
        email, password = _text(email, 200), (password or "")[:200]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", email) or not password:
            raise ModelError("geef het e-mailadres en het wachtwoord van je Brink Home-account")
        aid = brink.account_id(email)
        if aid in self.brink_accounts:
            raise ModelError("dit account staat er al")
        if len(self.brink_accounts) >= MAX_BRINK:
            raise ModelError(f"maximaal {MAX_BRINK} Brink Home-accounts")
        st = self._brink_new()
        try:
            await self._brink_login(st, {"email": email, "password": password})
            await self._brink_cycle(aid, st, {"email": email, "password": password}, flush=False)
        except brink.BrinkError as e:
            log.warning("Brink Home koppelen mislukt: %s", e)
            raise ModelError(e.reason) from None
        if not st["units"] and not st["systems"]:
            raise ModelError("dit account heeft geen systemen bij Brink Home")
        self.brink_accounts[aid] = {"name": _text(name, 60) or "Brink Home", "email": email, "password": password}
        self.bk[aid] = st
        st.update(online=True, error="", auth=False)
        self._save()
        self._start_brink(aid)
        self._poke()
        return aid

    def update_brink(self, aid: str, password: str, name: str | None = None) -> None:
        """Een nieuw wachtwoord (na een wijziging bij Brink): de volgende ronde logt meteen opnieuw in, ook als het account net was stilgezet na een onjuist wachtwoord."""
        acc = self.brink_accounts.get(aid)
        if not acc:
            raise ModelError("onbekend account")
        if not password and name is None:
            raise ModelError("geef een nieuw wachtwoord of een naam")
        if password:
            acc["password"] = password[:200]
        if name is not None:
            acc["name"] = _text(name, 60) or acc["name"]
        st = self.bk.get(aid)
        if st is not None:
            st.update(auth=False, token=None)
        self._save()
        self._kick(f"brink:{aid}")
        self._poke()

    async def remove_brink(self, aid: str) -> bool:
        """Het account en al zijn blokken weghalen. Het account bij Brink Home verandert niet."""
        if aid not in self.brink_accounts:
            return False
        for did in [k for k, v in self.devices.items() if v["driver"] == "brink" and v["account"] == aid]:
            await self.remove(did)
        t = self.tasks.pop(f"brink:{aid}", None)
        if t:
            t.cancel()
        self.wake.pop(f"brink:{aid}", None)
        del self.brink_accounts[aid]
        self.bk.pop(aid, None)
        self._save()
        self._poke()
        return True

    async def claim_brink(self, aid: str, keys: list[str] | None = None) -> list[str]:
        """Ventilatie-units van een account als blok toevoegen (`keys` leeg: alles wat er nog niet staat). Een sleutel is `<systeem>:<component>`."""
        st = self.bk.get(aid)
        if aid not in self.brink_accounts or not st or not st["units"]:
            raise ModelError("Brink Home is niet bereikbaar: er is nog geen lijst van units")
        have = {f"{v['sid']}:{v['cid']}" for v in self.devices.values() if v["driver"] == "brink" and v["account"] == aid}
        if keys:
            if any(k not in st["units"] for k in keys):
                raise ModelError("Brink Home kent deze unit niet (meer)")
            take = [k for k in keys if k not in have]
        else:
            take = [k for k in st["units"] if k not in have]
        if not take:
            raise ModelError("alles wat Brink Home aanbiedt staat er al")
        if len(self.devices) + len(take) > MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} apparaten")
        new = []
        for key in take:
            sid, cid = key.split(":")
            desc = brink.describe(st["units"][key])
            hid = brink.hid_of(sid, cid)
            did = _slug(hid)
            if did in self.devices:
                continue
            self.devices[did] = self._defaults({"hid": hid, "name": desc["name"], "driver": "brink", "account": aid, "sid": sid, "cid": cid, "type": desc["type"], "fw": desc["fw"]})
            self.brink_desc[did] = {**desc, "id": hid}
            self._apply_desc(did, self.brink_desc[did])
            new.append(did)
        self._save()
        if st["online"]:
            await self._brink_flush(aid)
        self._poke()
        return new

    def _start_brink(self, aid: str) -> None:
        if f"brink:{aid}" not in self.tasks:
            self.bk.setdefault(aid, self._brink_new())
            self.wake[f"brink:{aid}"] = asyncio.Event()
            self.tasks[f"brink:{aid}"] = asyncio.get_running_loop().create_task(self._run_brink(aid))

    @staticmethod
    def _brink_new() -> dict:
        return {"units": {}, "systems": {}, "token": None, "exp": 0.0, "online": False, "error": "", "auth": False, "burst": []}

    def _brink_kick(self, aid: str) -> None:
        """Na een opdracht: Brink voert hem asynchroon uit, dus over 2, 6 en 15 s kijken we of de unit er is."""
        st = self.bk.get(aid)
        if st is not None:
            st["burst"] = list(BRINK_BURST)
        self._kick(f"brink:{aid}")

    def _brink_children(self, aid: str) -> list[str]:
        return [did for did, dev in self.devices.items() if dev["driver"] == "brink" and dev["account"] == aid]

    async def _brink_login(self, st: dict, acc: dict) -> None:
        # een eigen sessie met een cookiejar voor het inloggen (de anti-forgery cookie); `unsafe` laat ook een IP-adres toe, voor de tests
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            token, ttl = await brink.login(s, acc["email"], acc["password"])
        st["token"], st["exp"] = token, time.monotonic() + ttl

    async def _brink_call(self, st: dict, acc: dict, method: str, path: str, body=None) -> tuple[int, object]:
        """Een verzoek met het toegangstoken; verlopen of ingetrokken (401) = een keer opnieuw inloggen en nog een keer proberen."""
        for attempt in (0, 1):
            if st["token"] is None or time.monotonic() > st["exp"] - BRINK_REAUTH:
                await self._brink_login(st, acc)
            try:
                return await brink.request(self._session(), st["token"], method, path, body)
            except brink.BrinkError as e:
                if e.code != "token" or attempt:
                    raise
                st["token"] = None
        raise brink.BrinkError("token", "de sessie bij Brink Home is verlopen")

    async def _brink_cycle(self, aid: str, st: dict, acc: dict, flush: bool = True) -> None:
        """Eén ronde: de systemen, en van elk systeem dat online is de beschrijving met de waarden. Een systeem dat offline is, geeft geen waarden (Brink toont daar zijn laatste stand)."""
        status, data = await self._brink_call(st, acc, "GET", f"{brink.API}systems?pageSize=50")
        if status != 200:
            raise brink.BrinkError("net", f"Brink Home antwoordt met HTTP {status}", status)
        systems = brink.systems(data)
        units: dict[str, dict] = {}
        for sid, info in systems.items():
            if info["state"] != brink.GATEWAY_ONLINE:
                continue
            status, ui = await self._brink_call(st, acc, "GET", f"{brink.API}systems/{sid}/uidescription")
            if status != 200:
                raise brink.BrinkError("net", f"Brink Home antwoordt met HTTP {status}", status)
            for cid, comp in brink.ventilation_units(brink.components(ui)).items():
                units[f"{sid}:{cid}"] = comp
        st["systems"], st["units"] = systems, units
        if flush:
            await self._brink_flush(aid)

    async def _brink_flush(self, aid: str) -> None:
        """De lijst van Brink als HUISDEV-state naar elk blok. Een unit die niet in de lijst staat is offline: de module meldt zich niet bij Brink, of staat niet meer in het account."""
        st = self.bk[aid]
        st.update(online=True, error="", auth=False)
        for did in self._brink_children(aid):
            dev = self.devices[did]
            comp = st["units"].get(f"{dev['sid']}:{dev['cid']}")
            if comp is None:
                info = st["systems"].get(dev["sid"])
                self._miss(did, "de module meldt zich niet bij Brink Home (offline)" if info and info["state"] != brink.GATEWAY_ONLINE else "deze unit staat niet meer in je Brink Home-account", hard=True)
                continue
            desc = {**brink.describe(comp, dev["name"]), "id": dev["hid"]}
            self.brink_desc[did] = desc
            vals = brink.values(comp)
            pts = {p["id"]: ({"v": vals[p["id"]], "age": 0} if vals.get(p["id"]) is not None else {"v": None}) for p in desc["points"]}
            await self._on_state(did, {"proto": "1.0", "rev": desc["rev"], "points": pts, "actions": {}})
        self._poke()

    def _brink_down(self, aid: str, st: dict, e: brink.BrinkError) -> None:
        st.update(online=False, error=str(e), auth=e.code == "auth")
        for did in self._brink_children(aid):
            self._miss(did, str(e), hard=True)  # wat we niet meer horen, laat geen waarde meer vers: meteen offline
        self._poke()

    async def _run_brink(self, aid: str) -> None:
        while True:
            acc = self.brink_accounts.get(aid)
            if not acc:
                return
            st = self.bk.setdefault(aid, self._brink_new())
            delay = None
            try:
                await self._brink_cycle(aid, st, acc)
            except brink.BrinkError as e:
                self._brink_down(aid, st, e)
                delay = BRINK_AUTH_WAIT if e.code == "auth" else BRINK_BACKOFF if e.code == "rate" else None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - een rare lijst mag het volgen van dit account niet voorgoed stilleggen
                log.exception("Brink %s: lijst niet te verwerken", aid)
                self._brink_down(aid, st, brink.BrinkError("net", "onbegrijpelijk antwoord"))
            if delay is None:
                delay = st["burst"].pop(0) if st["burst"] else BRINK_POLL
            ev = self.wake.get(f"brink:{aid}")
            woke = False
            try:
                await asyncio.wait_for(ev.wait(), delay) if ev else await asyncio.sleep(delay)
                woke = ev is not None
            except asyncio.TimeoutError:
                pass
            if ev:
                ev.clear()
            if woke and st["burst"] and not st["auth"]:  # een opdracht net verstuurd: eerst de tijd geven die de unit nodig heeft
                await asyncio.sleep(st["burst"].pop(0))

    async def _brink_send(self, did: str, dev: dict, pid: str, v) -> dict:
        acc, st = self.brink_accounts.get(dev["account"]), self.bk.get(dev["account"])
        if not acc or not st or did not in self.online:
            raise ModelError("niet verstuurd: geen verbinding met Brink Home")
        comp = st["units"].get(f"{dev['sid']}:{dev['cid']}")
        if comp is None:
            raise ModelError("deze unit staat niet meer in je Brink Home-account")
        try:
            path, body = brink.write_request(dev["sid"], comp, pid, v)
        except ValueError as e:
            raise ModelError(str(e)) from None
        try:
            status, data = await self._brink_call(st, acc, "PUT", path, body)
        except brink.BrinkError as e:
            raise ModelError(e.reason) from None
        if status not in (200, 204):
            raise ModelError(f"Brink Home weigert dit (HTTP {status})")
        self._brink_kick(dev["account"])
        return {"ok": True, "result": "queued"}  # Brink nam de opdracht aan; of de unit hem uitvoerde, zie je aan de stand bij de volgende poll

    def brink_view(self) -> list[dict]:
        """De Brink Home-accounts voor de pagina, met wat ze aanbieden. Nooit het wachtwoord of het e-mailadres."""
        out = []
        for aid, acc in self.brink_accounts.items():
            st = self.bk.get(aid) or self._brink_new()
            have = {f"{v['sid']}:{v['cid']}": k for k, v in self.devices.items() if v["driver"] == "brink" and v["account"] == aid}
            units = []
            for key, comp in st["units"].items():
                sid, cid = key.split(":")
                units.append({"key": key, "sid": sid, "cid": cid, "hid": brink.hid_of(sid, cid), "name": brink.describe(comp)["name"], "id": have.get(key), "claimed": key in have})
            out.append({"id": aid, "name": acc["name"], "online": st["online"], "error": st["error"], "auth": st["auth"], "units": sorted(units, key=lambda x: (x["name"].lower(), x["key"]))})
        return out

    # ------------------------------------------------------------ Smappee: een account (gebruikersnaam en wachtwoord van het dashboard) dat gepold wordt, en een blok per laadpunt

    @staticmethod
    def _smappee_keys(client_id, client_secret, budget) -> tuple[str, str, int | None]:
        """De sleutels van de officiele API (client-id en secret, samen of geen) en het budget (verzoeken per uur, 1 tot 20)."""
        cid, sec = _text(client_id, 200), (client_secret or "")[:200] if isinstance(client_secret, str) else ""
        if bool(cid) != bool(sec):
            raise ModelError("geef zowel de client-id als het secret van de API van Smappee, of geen van beide")
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= smappee.PUB_LIMIT):
            raise ModelError(f"het budget is een aantal verzoeken per uur, van 1 tot {smappee.PUB_LIMIT} (Smappee laat er {smappee.PUB_LIMIT} toe)")
        return cid, sec, budget

    async def add_smappee(self, username: str, password: str, name: str = "", client_id: str = "", client_secret: str = "", budget: int | None = None) -> str:
        """Een Smappee-account koppelen. Het wachtwoord wordt meteen gecontroleerd door in te loggen, en de sleutels van de officiele API (voor verbruik en teller) door er een keer mee te lezen. Geeft het id van het account."""
        username, password = _text(username, 200), (password or "")[:200]
        if not username or not password:
            raise ModelError("geef de gebruikersnaam en het wachtwoord van je Smappee-dashboard")
        client_id, client_secret, budget = self._smappee_keys(client_id, client_secret, budget)
        aid = smappee.account_id(username)
        if aid in self.smappee_accounts:
            raise ModelError("dit account staat er al")
        if len(self.smappee_accounts) >= MAX_SMAPPEE:
            raise ModelError(f"maximaal {MAX_SMAPPEE} Smappee-accounts")
        st, acc = self._smappee_new(), {"username": username, "password": password, "client_id": client_id, "client_secret": client_secret, "budget": budget or PUB_BUDGET}
        try:
            await self._smappee_login(st, acc)
            await self._smappee_cycle(aid, st, acc, flush=False)
        except smappee.SmappeeError as e:
            log.warning("Smappee koppelen mislukt: %s", e)
            raise ModelError(e.reason) from None
        if client_id and st.get("pub") and st["pub"]["error"]:
            raise ModelError(f"verbruik en teller: {st['pub']['error']}")
        if not st["units"]:
            why = self._smappee_explain(st)
            log.warning("Smappee: geen laadpunten gevonden (%s)", why)
            raise ModelError(f"dit account heeft geen laadpunten bij Smappee (of geen recht om ze te zien). Wat Smappee gaf: {why}")
        self.smappee_accounts[aid] = {"name": _text(name, 60) or "Smappee", **acc}
        self.sm[aid] = st
        st.update(online=True, error="", auth=False)
        self._save()
        self._start_smappee(aid)
        self._poke()
        return aid

    def update_smappee(self, aid: str, password: str, name: str | None = None, client_id: str | None = None, client_secret: str | None = None, budget: int | None = None) -> None:
        """Een nieuw wachtwoord (na een wijziging bij Smappee): de volgende ronde logt meteen opnieuw in, ook als het account net was stilgezet na een onjuist wachtwoord. Ook de sleutels van de officiele API
        (client-id en secret samen; beide leeg haalt verbruik en teller weg) en het budget (verzoeken per uur)."""
        acc = self.smappee_accounts.get(aid)
        if not acc:
            raise ModelError("onbekend account")
        keys = client_id is not None or client_secret is not None
        if not password and name is None and not keys and budget is None:
            raise ModelError("geef een nieuw wachtwoord, een naam, de sleutels van de API of een budget")
        if keys:
            cid, sec, _ = self._smappee_keys(client_id or "", client_secret or "", None)
        if budget is not None:
            self._smappee_keys("", "", budget)
        if password:
            acc["password"] = password[:200]
        if keys:
            self._pub_budgets.pop(acc["client_id"], None)
            acc["client_id"], acc["client_secret"] = cid, sec
            if aid in self.sm:
                self.sm[aid]["pub"] = None
        if budget is not None:
            acc["budget"] = budget
        if name is not None:
            acc["name"] = _text(name, 60) or acc["name"]
        st = self.sm.get(aid)
        if st is not None:
            st.update(auth=False, token=None)
        self._save()
        self._kick(f"smappee:{aid}")
        self._poke()

    async def remove_smappee(self, aid: str) -> bool:
        """Het account en al zijn blokken weghalen. Het account bij Smappee verandert niet."""
        if aid not in self.smappee_accounts:
            return False
        for did in [k for k, v in self.devices.items() if v["driver"] == "smappee" and v["account"] == aid]:
            await self.remove(did)
        t = self.tasks.pop(f"smappee:{aid}", None)
        if t:
            t.cancel()
        self.wake.pop(f"smappee:{aid}", None)
        del self.smappee_accounts[aid]
        self.sm.pop(aid, None)
        self._save()
        self._poke()
        return True

    async def claim_smappee(self, aid: str, keys: list[str] | None = None) -> list[str]:
        """Laadpunten van een account als blok toevoegen (`keys` leeg: alles wat er nog niet staat). Een sleutel is `<serienummer>:<smart device>`."""
        st = self.sm.get(aid)
        if aid not in self.smappee_accounts or not st or not st["units"]:
            raise ModelError("Smappee is niet bereikbaar: er is nog geen lijst van laadpunten")
        have = {f"{v['sid']}:{v['cid']}" for v in self.devices.values() if v["driver"] == "smappee" and v["account"] == aid}
        if keys:
            if any(k not in st["units"] for k in keys):
                raise ModelError("Smappee kent dit laadpunt niet (meer)")
            take = [k for k in keys if k not in have]
        else:
            take = [k for k in st["units"] if k not in have]
        if not take:
            raise ModelError("alles wat Smappee aanbiedt staat er al")
        if len(self.devices) + len(take) > MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} apparaten")
        new = []
        for key in take:
            unit = st["units"][key]
            desc = smappee.describe(unit)
            hid = smappee.hid_of(unit["serial"], unit["id"])
            did = _slug(hid)
            if did in self.devices:
                continue
            self.devices[did] = self._defaults({"hid": hid, "name": desc["name"], "driver": "smappee", "account": aid, "sid": unit["serial"], "cid": unit["id"], "type": desc["type"], "fw": desc["fw"]})
            self.smappee_desc[did] = {**desc, "id": hid}
            self._apply_desc(did, self.smappee_desc[did])
            new.append(did)
        self._save()
        if st["online"]:
            await self._smappee_flush(aid)
        self._poke()
        return new

    def _start_smappee(self, aid: str) -> None:
        if f"smappee:{aid}" not in self.tasks:
            self.sm.setdefault(aid, self._smappee_new())
            self.wake[f"smappee:{aid}"] = asyncio.Event()
            self.tasks[f"smappee:{aid}"] = asyncio.get_running_loop().create_task(self._run_smappee(aid))

    @staticmethod
    def _smappee_new() -> dict:
        return {"units": {}, "stations": {}, "struct_at": 0.0, "sessions": {}, "sess_at": {}, "sess_active": {}, "token": None, "exp": 0.0, "online": False, "error": "", "auth": False, "burst": [], "pub": None}

    def _smappee_kick(self, aid: str) -> None:
        """Na een opdracht: Smappee voert hem asynchroon uit, dus over 3, 10 en 30 s kijken we hoe het laadpunt erbij staat."""
        st = self.sm.get(aid)
        if st is not None:
            st["burst"] = list(SMAPPEE_BURST)
            st["sess_at"] = {}  # en de sessies ook opnieuw
        self._kick(f"smappee:{aid}")

    @staticmethod
    def _smappee_explain(st: dict) -> str:
        """Waar het zoeken naar laadpunten strandde, in woorden en getallen (geen namen, ids of tokens): wat het account aan Smappee-locaties heeft, hoeveel daarvan een laadstation hebben, hoeveel modules
        en laadpunten dat station meldt, en wat de lijst met smart devices bevat."""
        d = st.get("diag") or {}
        parts = [f"{d.get('locations', '?')} locaties" + (f" (velden: {', '.join(d['keys'])})" if d.get("keys") else ""), f"{len(st.get('stations') or {})} met een laadstation"]
        for s, x in (d.get("stations") or {}).items():
            parts.append(f"station …{s}: {x['modules']}, {x['laadpunten']} laadpunten")
        for loc, x in (d.get("devices") or {}).items():
            parts.append(f"locatie …{loc}: {x['n']} smart devices (categorieën: {', '.join(x['categorieen']) or 'geen'})")
        return "; ".join(parts)

    def _smappee_children(self, aid: str) -> list[str]:
        return [did for did, dev in self.devices.items() if dev["driver"] == "smappee" and dev["account"] == aid]

    async def _smappee_login(self, st: dict, acc: dict) -> None:
        token, ttl = await smappee.login(self._session(), acc["username"], acc["password"])
        st["token"], st["exp"] = token, time.monotonic() + ttl

    async def _smappee_call(self, st: dict, acc: dict, method: str, path: str, body=None, params: dict | None = None) -> tuple[int, object]:
        """Een verzoek met het toegangstoken; verlopen of ingetrokken (401) = een keer opnieuw inloggen en nog een keer proberen."""
        for attempt in (0, 1):
            if st["token"] is None or time.monotonic() > st["exp"] - SMAPPEE_REAUTH:
                await self._smappee_login(st, acc)
            try:
                return await smappee.request(self._session(), st["token"], method, path, body, params)
            except smappee.SmappeeError as e:
                if e.code != "token" or attempt:
                    raise
                st["token"] = None
        raise smappee.SmappeeError("token", "de sessie bij Smappee is verlopen")

    async def _smappee_structure(self, st: dict, acc: dict) -> None:
        """De laadstations van het account en hun laadpunten. Verandert zelden, dus maar eens per half uur (en bij het koppelen)."""
        status, data = await self._smappee_call(st, acc, "GET", f"{smappee.API}/v11/user/servicelocations", params={"fullDetails": "true"})
        if status != 200:
            raise smappee.SmappeeError("net", f"Smappee antwoordt met HTTP {status}", status)
        stations = smappee.stations(data)
        st["diag"] = {"locations": len(data) if isinstance(data, list) else f"geen lijst ({type(data).__name__})", "stations": {}, "devices": {}}
        if isinstance(data, list):
            st["diag"]["keys"] = sorted({k for x in data if isinstance(x, dict) for k in x})[:25]
        for serial, stn in stations.items():
            status, det = await self._smappee_call(st, acc, "GET", f"{smappee.API}/v10/chargingstations/{serial}", params={"includeDetails": "true"})
            if status != 200:
                raise smappee.SmappeeError("net", f"Smappee antwoordt met HTTP {status} over laadstation {serial}", status)
            stn["connectors"] = smappee.connectors(det)
            st["diag"]["stations"][serial[-4:]] = {"modules": smappee.module_shape(det), "laadpunten": len(stn["connectors"])}
            stn["available"] = det.get("available") if isinstance(det, dict) and isinstance(det.get("available"), bool) else None
        st["stations"], st["struct_at"] = stations, time.monotonic()

    async def _smappee_cycle(self, aid: str, st: dict, acc: dict, flush: bool = True) -> None:
        """Eén ronde: de lijst (zo nodig), de laadpunten met hun stand, en de sessies als die aan de beurt zijn."""
        now = time.monotonic()
        if not st["stations"] or now - st["struct_at"] >= SMAPPEE_STRUCT:
            await self._smappee_structure(st, acc)
        units: dict[str, dict] = {}
        by_loc: dict[str, dict] = {}
        raw_by_loc: dict[str, object] = {}
        for serial, stn in st["stations"].items():
            if stn["loc"] not in by_loc:
                status, data = await self._smappee_call(st, acc, "GET", f"{smappee.API}/v10/servicelocation/{stn['loc']}/homecontrol/smart/devices", params={"excludedCategories": ""})
                if status != 200:
                    raise smappee.SmappeeError("net", f"Smappee antwoordt met HTTP {status}", status)
                by_loc[stn["loc"]] = smappee.device_map(data)
                raw_by_loc[stn["loc"]] = data
                st.setdefault("diag", {}).setdefault("devices", {})[stn["loc"][-4:]] = {"n": len(data) if isinstance(data, list) else f"geen lijst ({type(data).__name__})", "categorieen": smappee.categories(data)}
            if not stn["connectors"]:  # de modules van het station wezen er geen aan: dan de lijst met smart devices zelf (categorie CARCHARGER)
                stn["connectors"] = smappee.connectors_from_devices(raw_by_loc.get(stn["loc"]))
                if stn["connectors"]:
                    log.info("Smappee %s: de modules van het laadstation wezen geen laadpunt aan; %d uit de lijst met smart devices", serial[-4:], len(stn["connectors"]))
            for c in stn["connectors"]:
                dev = by_loc[stn["loc"]].get(c["id"]) or (by_loc[stn["loc"]].get(c["uuid"]) if c["uuid"] else None)
                if dev is not None:
                    units[f"{serial}:{c['id']}"] = {**c, "serial": serial, "loc": stn["loc"], "dev": dev, "station_available": stn.get("available"), "only": len(stn["connectors"]) == 1, "pub": bool(acc.get("client_id"))}
        st["units"] = units
        for serial in st["stations"]:
            mine = [u for u in units.values() if u["serial"] == serial]
            active = any(smappee.state_of(u["dev"]) in (smappee.S_CHARGING, smappee.S_PAUSED) for u in mine)
            due = now - st["sess_at"].get(serial, -1e9) >= (SMAPPEE_SESS_ACTIVE if active else SMAPPEE_SESS_IDLE) or st["sess_active"].get(serial) != active
            if not mine or not due:
                continue
            frm = int((time.time() - smappee.SESSION_DAYS * 86400) * 1000)
            try:
                status, data = await self._smappee_call(st, acc, "GET", f"{smappee.API}/v10/chargingstations/{serial}/sessions", params={"range": f"{frm},{int(time.time() * 1000)}", "rangeMode": "stop_or_start"})
            except smappee.SmappeeError as e:
                if e.code in ("rate", "auth", "maintenance"):
                    raise
                log.warning("Smappee %s: sessies niet te lezen: %s", serial, e)
                continue
            if status != 200 or not isinstance(data, list):
                log.warning("Smappee %s: sessies niet te lezen (HTTP %s)", serial, status)
                continue
            for u in mine:
                st["sessions"][f"{serial}:{u['id']}"] = smappee.sessions(data, u, u["only"])
                self._smappee_note(st, u, now)
            st["sess_at"][serial], st["sess_active"][serial] = now, active
        await self._smappee_pub(st, acc)
        if flush:
            await self._smappee_flush(aid)

    async def _smappee_flush(self, aid: str) -> None:
        """De lijst van Smappee als HUISDEV-state naar elk blok. Een laadpunt dat niet in de lijst staat is weg uit het account."""
        st = self.sm[aid]
        st.update(online=True, error="", auth=False)
        for did in self._smappee_children(aid):
            dev = self.devices[did]
            key = f"{dev['sid']}:{dev['cid']}"
            unit = st["units"].get(key)
            if unit is None:
                self._miss(did, "dit laadpunt staat niet meer in je Smappee-account", hard=True)
                continue
            desc = {**smappee.describe(unit, dev["name"]), "id": dev["hid"]}
            self.smappee_desc[did] = desc
            sess = st["sessions"].get(key) or []
            power, meter = self._smappee_pubvals(st, unit)
            vals = smappee.values(unit, sess[0]["kwh"] if sess else None, power, meter)
            pts = {p["id"]: ({"v": vals[p["id"]], "age": 0} if vals.get(p["id"]) is not None else {"v": None}) for p in desc["points"]}
            await self._on_state(did, {"proto": "1.0", "rev": desc["rev"], "points": pts, "actions": {}})
        self._poke()

    # ---- verbruik en teller: de officiele API, met een budget (hoogstens 20 verzoeken per uur, ook het token telt)

    def _smappee_budget(self, acc: dict) -> smappee.Budget:
        b = self._pub_budgets.get(acc["client_id"])
        if b is None:
            b = self._pub_budgets[acc["client_id"]] = smappee.Budget(self.path.with_name("smappee_budget.json"), acc["client_id"], acc.get("budget") or PUB_BUDGET)
        b.limit = max(1, min(int(acc.get("budget") or PUB_BUDGET), smappee.PUB_LIMIT))
        return b

    @staticmethod
    def _smappee_note(st: dict, unit: dict, now: float) -> None:
        """De energie van de lopende sessie van een laadpunt onthouden (alleen als ze veranderde), voor het geschatte vermogen. Zonder lopende sessie is er niets te onthouden."""
        key = f"{unit['serial']}:{unit['id']}"
        sess = st["sessions"].get(key) or []
        run = sess[0] if sess and sess[0]["end"] is None and sess[0]["kwh"] is not None else None
        e = st.setdefault("energy", {})
        if run is None:
            e.pop(key, None)
            return
        cur = e.get(key)
        if cur is None or cur["key"] != run["key"]:
            cur = e[key] = {"key": run["key"], "events": []}
        cur["events"] = smappee.note_energy(cur["events"], now, run["kwh"])

    @staticmethod
    def _smappee_pubvals(st: dict, unit: dict) -> tuple[float | None, float | None]:
        """Verbruik (W) en teller (kWh) voor een laadpunt, of None.
        Verbruik: niet aan het laden (status bekend) is 0; aan het laden is het geschat uit de groei van de sessie-energie, en anders (of als dat nog niet kan) het laatste blok van de officiele API; is de status onbekend, dan alleen dat blok.
        Teller: de laatste waarde van de officiele API plus wat de sessies sindsdien erbij kregen. Het verbruik van de API geldt voor de hele servicelocatie en is dus onbekend bij een laadstation met twee laadpunten."""
        now, key = time.monotonic(), f"{unit['serial']}:{unit['id']}"
        state = smappee.state_of(unit["dev"])
        p = ((st.get("pub") or {}).get("loc") or {}).get(unit["loc"]) if unit.get("pub") and unit.get("only") else None
        if state is not None and state != smappee.S_CHARGING:
            power = 0.0
        else:
            power = smappee.virtual_power(((st.get("energy") or {}).get(key) or {}).get("events") or [], now) if state == smappee.S_CHARGING and unit.get("only") else None
            if power is None and p and p["rate_at"] is not None and now - p["rate_at"] <= PUB_STALE_RATE:
                power = p["rate"]
        meter = None
        if p and p["meter_at"] is not None and now - p["meter_at"] <= PUB_STALE_METER:
            est = smappee.virtual_meter(p["meter"], p.get("base"), st["sessions"].get(key) or [])
            meter = est if est is not None else p["meter"]
        return power, meter

    def _smappee_detail(self, st: dict, pub: dict | None) -> dict:
        """Wat de officiele API gaf (getallen), en waar de teller van nu uit bestaat: de waarde van de API en wat de sessies sindsdien erbij kregen."""
        if not pub:
            return {}
        out = {}
        for loc, p in pub["loc"].items():
            u = next((x for x in st["units"].values() if x["loc"] == loc and x.get("only")), None)
            _power, meter = self._smappee_pubvals(st, u) if u else (None, None)
            out = {"rate": p.get("diag_rate"), "meter": p.get("diag_meter"), "meter_api": p["meter"], "meter_shown": meter, "rate_api": p["rate"],
                   "meter_age_s": int(time.monotonic() - p["meter_at"]) if p["meter_at"] is not None else None, "rate_age_s": int(time.monotonic() - p["rate_at"]) if p["rate_at"] is not None else None}
            break
        return out

    async def _smappee_pub_read(self, pub: dict, acc: dict, budget: smappee.Budget, loc: str, aggregation: int, frm_ms: int) -> object | None:
        """Een keer lezen bij de officiele API, binnen het budget (het token en het verzoek tellen allebei). Geeft de JSON, of None als er geen budget is."""
        for attempt in (0, 1):
            need = 1 if pub["token"] and time.monotonic() < pub["exp"] - 60 else 2
            if not budget.can(need):
                return None
            if need == 2:
                budget.spend()
                pub["token"] = None
                tok, ttl = await smappee.pub_token(self._session(), acc["username"], acc["password"], acc["client_id"], acc["client_secret"])
                pub["token"], pub["exp"] = tok, time.monotonic() + ttl
            budget.spend()
            try:
                status, data = await smappee.pub_consumption(self._session(), pub["token"], loc, aggregation, frm_ms, int(time.time() * 1000))
            except smappee.SmappeeError as e:
                if e.code == "token" and attempt == 0:
                    pub["token"] = None  # een keer opnieuw een token, als het budget het toelaat
                    continue
                raise
            if status != 200:
                raise smappee.SmappeeError("net", f"de API van Smappee antwoordt met HTTP {status}", status)
            return data
        return None

    async def _smappee_pub(self, st: dict, acc: dict) -> None:
        """Het verbruik en de teller van elk laadstation, als de sleutels van de officiele API bekend zijn en het budget het toelaat. Een fout stopt de ronde van het dashboard niet."""
        if not acc.get("client_id"):
            return
        pub = st["pub"] = st.get("pub") or {"token": None, "exp": 0.0, "hold": 0.0, "error": "", "auth": False, "loc": {}}
        now = time.monotonic()
        if now < pub["hold"]:
            return
        budget = self._smappee_budget(acc)
        for serial, stn in st["stations"].items():
            loc = stn["loc"]
            p = pub["loc"].setdefault(loc, {"rate": None, "rate_at": None, "meter": None, "meter_at": None, "next_rate": 0.0, "next_meter": 0.0, "active": False, "base": None})
            active = any(smappee.state_of(u["dev"]) in (smappee.S_CHARGING, smappee.S_PAUSED) for u in st["units"].values() if u["serial"] == serial)
            if p["active"] and not active and (p["meter_at"] is None or now - p["meter_at"] >= PUB_MIN_GAP):
                p["next_meter"] = 0.0  # een sessie is net klaar: de teller opnieuw
            p["active"] = active
            for kind in ("rate", "meter"):
                if time.monotonic() < p[f"next_{kind}"]:
                    continue
                t0 = time.time()
                try:
                    data = await self._smappee_pub_read(pub, acc, budget, loc, smappee.AGG_5MIN if kind == "rate" else smappee.AGG_YEAR,
                                                        int(time.time() * 1000) - 15 * 60 * 1000 if kind == "rate" else smappee.PUB_METER_FROM)
                except smappee.SmappeeError as e:
                    pub["error"] = e.reason
                    pub["token"] = None if e.code in ("auth", "token") else pub["token"]
                    p[f"next_{kind}"] = time.monotonic() + PUB_RETRY
                    if e.code in ("auth", "rate", "maintenance"):
                        pub["auth"] = e.code == "auth"
                        pub["hold"] = time.monotonic() + (PUB_HOLD_AUTH if e.code == "auth" else max(e.wait, PUB_HOLD_RATE) if e.code == "rate" else PUB_HOLD_MAINT)
                        log.warning("Smappee verbruik: %s; %d s wachten", e.reason, pub["hold"] - time.monotonic())
                        return
                    log.warning("Smappee verbruik: %s", e.reason)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - een rare lijst mag het volgen van dit account niet stilleggen
                    log.exception("Smappee verbruik niet te verwerken")
                    pub["error"] = "onbegrijpelijk antwoord van de API van Smappee"
                    p[f"next_{kind}"] = time.monotonic() + PUB_RETRY
                    continue
                if data is None:  # geen budget meer dit uur: straks opnieuw
                    break
                at = time.monotonic()
                p[f"diag_{kind}"] = smappee.shape_of(data, time.time() * 1000)
                if kind == "rate":
                    p["rate"], p["rate_at"], p["next_rate"] = smappee.rate_w(data, time.time() * 1000), at, at + (PUB_RATE_ACTIVE if active else PUB_RATE_IDLE)
                else:
                    p["meter"], p["meter_at"], p["next_meter"] = smappee.meter_kwh(data), at, at + PUB_METER_EVERY
                    mine = [u for u in st["units"].values() if u["serial"] == serial]
                    if len(mine) == 1:  # wat er bij deze lezing liep: de teller van nu is deze waarde plus wat de sessies sindsdien erbij kregen
                        sess = st["sessions"].get(f"{serial}:{mine[0]['id']}") or []
                        run = sess[0] if sess and sess[0]["end"] is None else None
                        p["base"] = {"wall": t0, "key": run["key"] if run else None, "kwh": (run["kwh"] or 0.0) if run else 0.0}
                pub["error"], pub["auth"] = "", False

    def _smappee_down(self, aid: str, st: dict, e: smappee.SmappeeError) -> None:
        st.update(online=False, error=str(e), auth=e.code == "auth")
        for did in self._smappee_children(aid):
            self._miss(did, str(e), hard=True)  # wat we niet meer horen, laat geen waarde meer vers: meteen offline
        self._poke()

    async def _run_smappee(self, aid: str) -> None:
        while True:
            acc = self.smappee_accounts.get(aid)
            if not acc:
                return
            st = self.sm.setdefault(aid, self._smappee_new())
            delay = None
            try:
                await self._smappee_cycle(aid, st, acc)
            except smappee.SmappeeError as e:
                self._smappee_down(aid, st, e)
                delay = SMAPPEE_AUTH_WAIT if e.code == "auth" else max(e.wait, SMAPPEE_BACKOFF) if e.code == "rate" else None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - een rare lijst mag het volgen van dit account niet voorgoed stilleggen
                log.exception("Smappee %s: lijst niet te verwerken", aid)
                self._smappee_down(aid, st, smappee.SmappeeError("net", "onbegrijpelijk antwoord"))
            if delay is None:
                delay = st["burst"].pop(0) if st["burst"] else SMAPPEE_POLL
            ev = self.wake.get(f"smappee:{aid}")
            woke = False
            try:
                await asyncio.wait_for(ev.wait(), delay) if ev else await asyncio.sleep(delay)
                woke = ev is not None
            except asyncio.TimeoutError:
                pass
            if ev:
                ev.clear()
            if woke and st["burst"] and not st["auth"]:  # een opdracht net verstuurd: eerst de tijd geven die de lader nodig heeft
                await asyncio.sleep(st["burst"].pop(0))

    async def _smappee_send(self, did: str, dev: dict, pid: str, v) -> dict:
        acc, st = self.smappee_accounts.get(dev["account"]), self.sm.get(dev["account"])
        if not acc or not st or did not in self.online:
            raise ModelError("niet verstuurd: geen verbinding met Smappee")
        unit = st["units"].get(f"{dev['sid']}:{dev['cid']}")
        if unit is None:
            raise ModelError("dit laadpunt staat niet meer in je Smappee-account")
        try:
            req = smappee.write_request(unit, pid, v)
        except ValueError as e:
            raise ModelError(str(e)) from None
        if req is None:
            return {"ok": True, "result": "unchanged"}  # staat al zo: hervatten zou de lader op Standaard zetten, dus dan sturen we niets
        now = time.monotonic()
        if now - self._smappee_sent.get((did, pid), -1e9) < SMAPPEE_WRITE_GAP:
            raise ModelError(f"te snel na de vorige opdracht voor dit punt: wacht {SMAPPEE_WRITE_GAP:g} seconden")
        self._smappee_sent[(did, pid)] = now
        method, path, body = req
        try:
            status, _data = await self._smappee_call(st, acc, method, path, body)
        except smappee.SmappeeError as e:
            raise ModelError(e.reason) from None
        if status not in (200, 201, 204):
            raise ModelError(f"Smappee weigert dit (HTTP {status})")
        self._smappee_kick(dev["account"])
        return {"ok": True, "result": "queued"}  # Smappee nam de opdracht aan; of de lader hem uitvoerde, zie je aan de stand bij de volgende poll

    def smappee_sessions(self, did: str) -> list[dict] | None:
        """De sessies van een Smappee-laadpunt (nieuwste eerst, zie smappee.sessions) voor het laadblok, of None: geen Smappee-blok, of Smappee is nu niet bereikbaar."""
        dev = self.devices.get(did)
        st = self.sm.get(dev["account"]) if dev and dev["driver"] == "smappee" else None
        if not st or not st["online"]:
            return None
        return list(st["sessions"].get(f"{dev['sid']}:{dev['cid']}") or [])

    def point_value(self, did: str, pid: str):
        """De waarde van een punt nu (None: onbekend, offline of geen punt), voor het laadblok."""
        dev = self.devices.get(did)
        p = self._by_id(dev, pid) if dev else None
        return self._value_of(did, p) if p else None

    def smappee_view(self) -> list[dict]:
        """De Smappee-accounts voor de pagina, met wat ze aanbieden. Nooit het wachtwoord of de gebruikersnaam."""
        out = []
        for aid, acc in self.smappee_accounts.items():
            st = self.sm.get(aid) or self._smappee_new()
            have = {f"{v['sid']}:{v['cid']}": k for k, v in self.devices.items() if v["driver"] == "smappee" and v["account"] == aid}
            units = []
            for key, u in st["units"].items():
                units.append({"key": key, "serial": u["serial"], "cid": u["id"], "hid": smappee.hid_of(u["serial"], u["id"]), "name": smappee.describe(u)["name"], "id": have.get(key), "claimed": key in have,
                              "diag": smappee.diagnose(u, st["sessions"].get(key) or [], ((st.get("energy") or {}).get(key) or {}).get("events") or [], time.monotonic(), time.time())})
            pub = st.get("pub")
            b = self._smappee_budget(acc) if acc.get("client_id") else None
            out.append({"id": aid, "name": acc["name"], "online": st["online"], "error": st["error"], "auth": st["auth"], "units": sorted(units, key=lambda x: (x["name"].lower(), x["key"])),
                        "pub": {"on": bool(acc.get("client_id")), "error": pub["error"] if pub else "", "auth": bool(pub and pub["auth"]), "used": b.used() if b else 0, "limit": b.limit if b else 0,
                                "detail": self._smappee_detail(st, pub)}})
        return out

    async def _refetch(self, did: str) -> None:
        dev = self.devices[did]
        if dev["driver"] in ("esphome", "hue", "nuki", "brink", "smappee"):  # de beschrijving komt uit de eventstroom of de poll en is al in `_es_flush`, `_hue_flush`, `_nuki_flush` of `_brink_flush` gemaakt
            self._apply_desc(did, {"esphome": lambda: self.es[did]["desc"], "hue": lambda: self.hue_desc[did], "nuki": lambda: self.nuki_desc[did], "brink": lambda: self.brink_desc[did], "smappee": lambda: self.smappee_desc[did]}[dev["driver"]]())
            self._save()
            self._poke()
            return
        desc = await self._fetch_descriptor(dev)
        if desc["id"] != dev["hid"]:
            raise EspError(0, "descriptor", f"op dit adres zit nu {desc['id']}, niet {dev['hid']}")
        if proto_state(desc["proto"]) == "unsupported":
            raise EspError(0, "proto", f"protocol {desc['proto']} wordt niet ondersteund")
        self._apply_desc(did, desc)
        self._save()
        self._poke()

    def _apply_desc(self, did: str, desc: dict) -> None:
        """Een nieuwe descriptor: nieuwe punten erbij, verdwenen punten 30 dagen 'niet beschikbaar', een gewijzigd type als nieuw punt."""
        dev = self.devices[did]
        now = time.time()
        if dev["shown"] is None and dev["points"]:
            dev["shown"] = self.shown(dev)  # het blok verspringt niet als er punten komen of gaan: wat er stond, blijft staan
        old = {p["id"]: p for p in dev["points"]}
        points = []
        for p in desc["points"]:
            o = old.pop(p["id"], None)
            if o and o["kind"] != p["kind"]:
                log.error("%s: punt %s wisselde van type (%s naar %s); het protocol verbiedt dat. Het geldt als een nieuw punt", dev["name"], p["id"], o["kind"], p["kind"])
                dev["inputs"].pop(p["id"], None)
                self.seen.get(did, {}).pop(p["id"], None)
            points.append(p)
        for pid, o in old.items():
            if "gone" not in o:
                log.info("%s: punt %s is verdwenen", dev["name"], pid)
                o = {**o, "gone": now}
            if now - o["gone"] < GONE_KEEP:
                points.append(o)
            else:
                self._retire(did, dev, pid)
                dev["inputs"].pop(pid, None)
        for a in dev["actions"]:
            if a["id"] not in {x["id"] for x in desc["actions"]}:
                self._retire(did, dev, action=a["id"])
        dev.update(points=points, actions=desc["actions"], rev=desc["rev"], proto=desc["proto"], fw=desc["fw"] or dev["fw"], type=desc["type"] or dev["type"])
        if "emap" in desc:  # ESPHome: de toewijzing blijft ook voor een verdwenen punt (30 dagen), zodat zijn id niet voor iets anders wordt gebruikt
            live = {p["id"] for p in points} | {a["id"] for a in desc["actions"]}
            dev["emap"] = {pid: e for pid, e in {**dev["emap"], **desc["emap"]}.items() if pid in live}
            dev["skipped"] = desc.get("skipped", [])
        if dev["shown"] is not None:
            dev["shown"] = [i for i in dev["shown"] if any(p["id"] == i for p in points)]

    def _poke(self) -> None:
        if self.changed:
            self.changed()
        self.engine._emit("espdev", log=False)

    # ------------------------------------------------------------ de stand

    def is_online(self, did: str) -> bool:
        return did in self.online

    def _value_of(self, did: str, p: dict):
        """De waarde van een punt nu, of None: offline, weg, nog niet gemeld, of verouderd (`stale`)."""
        if did not in self.online or p.get("gone"):
            return None
        e = self.values.get(did, {}).get(p["id"])
        if not e or e["v"] is None:
            return None
        if p["stale"] and e["age"] + (time.monotonic() - e["at"]) * 1000 > p["stale"]:
            return None
        return e["v"]

    def output(self, ref: str):
        """De waarde op een uitgang `e:<id>:<punt>`, of None (onbekend: offline, weg, tekst of niet gemeld)."""
        m = REF_RE.match(ref or "")
        dev = self.devices.get(m.group(1)) if m else None
        p = self._by_id(dev, m.group(2)) if dev else None
        if not p or p["kind"] == "text":
            return None
        return self._value_of(m.group(1), p)

    def proto_state_of(self, dev: dict) -> str:
        return proto_state(dev["proto"])

    # ------------------------------------------------------------ bedienen

    async def _post(self, did: str, path: str, body: dict) -> dict:
        dev = self.devices.get(did)
        if not dev:
            raise ModelError("onbekend apparaat")
        if did not in self.online:
            raise ModelError("niet verstuurd: geen verbinding met het apparaat")
        try:
            status, data = await self._call(dev, "POST", path, body)
        except EspError as e:
            raise ModelError(str(e)) from None
        if status != 200:
            code = data.get("error") if isinstance(data.get("error"), str) else str(status)
            why = ERRORS.get(code, code)
            if data.get("reason") and code in ("disabled", "bad_value", "range", "step", "missing_arg"):
                why = f"{why}: {data['reason']}"
            raise ModelError(why)
        if data.get("result") == "rejected":
            raise ModelError(data.get("reason") or "het apparaat weigert dit")
        self._kick(did)  # de nieuwe stand meteen ophalen
        return data

    async def set(self, did: str, pid: str, value, confirm: bool = False) -> dict:
        """Een punt instellen, uit de zijbalk of uit HA. De stand verandert pas als het apparaat hem meldt."""
        dev = self.devices.get(did)
        if not dev:
            raise ModelError("onbekend apparaat")
        p = self._by_id(dev, pid)
        if not p:
            raise ModelError("onbekend punt")
        if not p["writable"]:
            raise ModelError(f"{p['label']} is niet instelbaar")
        if p.get("gone"):
            raise ModelError(f"{p['label']} is niet beschikbaar")
        if p["danger"] and not confirm:
            raise ModelError(p["confirm"] or "bevestiging nodig")
        v = check_value(p, value)
        if dev["driver"] == "hue":
            return await self._hue_send(did, dev, pid, v)
        if dev["driver"] == "brink":
            return await self._brink_send(did, dev, pid, v)
        if dev["driver"] == "smappee":
            return await self._smappee_send(did, dev, pid, v)
        if dev["driver"] == "esphome":
            eid = dev["emap"].get(pid)
            if not eid:
                raise ModelError(f"{p['label']} is niet beschikbaar")
            try:
                path, params = esp.point_request(eid, p, self.es.get(did, {}).get("ent", {}).get(eid), v)
            except ValueError as e:
                raise ModelError(f"{p['label']}: {e}") from None
            return await self._es_send(did, path, params)
        return await self._post(did, "/huisdev/set", {"id": pid, "value": v, **({"confirm": True} if p["danger"] else {})})

    async def do(self, did: str, aid: str, args: dict | None = None, confirm: bool = False) -> dict:
        dev = self.devices.get(did)
        if not dev:
            raise ModelError("onbekend apparaat")
        a = next((x for x in dev["actions"] if x["id"] == aid), None)
        if not a:
            raise ModelError("onbekende actie")
        if aid in self.off.get(did, {}):
            raise ModelError(f"{a['label']} kan nu niet" + (f": {self.off[did][aid]}" if self.off[did][aid] else ""))
        if a["danger"] and not confirm:
            raise ModelError(a["confirm"] or "bevestiging nodig")
        args = args if isinstance(args, dict) else {}
        out = {}
        for arg in a["args"]:
            if arg["id"] not in args:
                raise ModelError(f"{arg['label']}: geen waarde")
            out[arg["id"]] = check_value(arg, args[arg["id"]])
        if dev["driver"] == "nuki":
            return await self._nuki_send(did, dev, aid)
        if dev["driver"] == "esphome":
            eid = dev["emap"].get(aid)
            if not eid:
                raise ModelError(f"{a['label']} is niet beschikbaar")
            return await self._es_send(did, *esp.action_request(eid))
        return await self._post(did, "/huisdev/do", {"id": aid, "args": out, **({"confirm": True} if a["danger"] else {})})

    # ------------------------------------------------------------ de ingangen (zoals de airco: alleen bij een wijziging)

    async def _ticker(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - een fout in één ronde mag de ingangen niet voorgoed stilleggen
                log.exception("ingangen van zelfbouwapparaten")
            await asyncio.sleep(TICK)

    def tick(self) -> None:
        now = time.monotonic()
        for did, dev in self.devices.items():
            mem = self.seen.setdefault(did, {})
            for pid, ref in list(dev["inputs"].items()):
                act = self._wired_action(dev, pid)
                if act is not None:
                    self._tick_action(did, dev, pid, act, ref, mem, now)
                    continue
                p = self._by_id(dev, pid)
                if not p or not p["writable"] or p["danger"] or p["kind"] == "text" or p.get("gone"):
                    continue
                raw = self.engine.ref_value(ref)
                if p["kind"] == "bool":
                    v = raw if isinstance(raw, bool) else None
                else:
                    v = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
                if v is None:
                    mem.pop(pid, None)  # na onbekend telt de volgende stand weer als eerste stand
                    continue
                if pid not in mem:
                    mem[pid] = v
                    continue
                if v == mem[pid] or (did, pid) in self._inflight or did not in self.online:
                    continue  # offline: de wijziging wacht tot het apparaat terug is
                failed = self._failed.get((did, pid))
                if failed is not None and now - failed < RETRY:
                    continue
                self._inflight.add((did, pid))
                asyncio.get_running_loop().create_task(self._send_input(did, pid, p, v))

    @staticmethod
    def _wired_action(dev: dict, key: str) -> dict | None:
        """De actie achter een ingang `do_<actie>`: alleen bij een Nuki-slot en zonder argumenten (een actie met een argument kent geen 0/1)."""
        if dev["driver"] != "nuki" or not key.startswith(ACT_PREFIX):
            return None
        a = next((x for x in dev["actions"] if x["id"] == key[len(ACT_PREFIX):]), None)
        return a if a and not a["args"] else None

    def _tick_action(self, did: str, dev: dict, key: str, act: dict, ref: str, mem: dict, now: float) -> None:
        """Een ingang die een actie start. Strenger dan een ingang die een punt instelt, want het kan een deur zijn:
        - alleen bij de overgang van 0 naar 1; de eerste stand (na een herstart, of na onbekend) en 1 naar 0 doen niets;
        - een actie wacht nooit: is het apparaat niet bereikbaar, dan vervalt hij (een deur die tien minuten later opengaat is erger dan een deur die dicht blijft);
        - nooit opnieuw proberen na een mislukking, en hoogstens eens per `ACTION_GAP`."""
        raw = self.engine.ref_value(ref)
        v = raw if isinstance(raw, bool) else None
        if v is None:
            mem.pop(key, None)
            return
        was = mem.get(key)
        mem[key] = v
        if was is not False or v is not True:
            return
        label, last = act["label"], self.last.setdefault(did, {})
        if now - self._act_last.get((did, act["id"]), -1e9) < ACTION_GAP:
            last[key] = f"{label}: niet verstuurd (de vorige keer was minder dan {int(ACTION_GAP)} s geleden)"
            self._poke()
            return
        if did not in self.online:
            last[key] = f"{label}: niet verstuurd, want er is geen verbinding; de opdracht vervalt"
            self._poke()
            return
        self._act_last[(did, act["id"])] = now
        asyncio.get_running_loop().create_task(self._send_action_input(did, dev, key, act, ref))

    async def _send_action_input(self, did: str, dev: dict, key: str, act: dict, ref: str) -> None:
        label = act["label"]
        try:
            await self.do(did, act["id"], confirm=True)  # de aansluiting is de bevestiging
            self.last.setdefault(did, {})[key] = f"{label}: verstuurd"
            log.info("%s: %s gestart door %s", dev["name"], label, ref)  # een spoor van elke deur die een ingang opent
            self.engine._emit("espdev", what="action", device=did, action=act["id"], log=False)
        except ModelError as e:
            self.last.setdefault(did, {})[key] = f"{label}: niet verstuurd ({e}); niet opnieuw geprobeerd"
            log.warning("%s: %s door %s mislukte: %s", dev["name"], label, ref, e)
        finally:
            self._poke()

    async def _send_input(self, did: str, pid: str, p: dict, v) -> None:
        label = p["label"]
        try:
            if p["kind"] == "enum":
                sent = int(round(v))
                if sent not in {o["v"] for o in p["options"]}:
                    self.seen.setdefault(did, {})[pid] = v
                    self.last.setdefault(did, {})[pid] = f"{label}: {sent} is geen keuze, niets verstuurd"
                    return
            elif p["kind"] == "bool":
                sent = v
            else:
                sent = _snap(p, v)
            await self.set(did, pid, sent)
            self._failed.pop((did, pid), None)
            self.seen.setdefault(did, {})[pid] = v
            self.last.setdefault(did, {})[pid] = f"{label} → {'1' if sent is True else '0' if sent is False else f'{sent:g}'}: verstuurd"
            self.engine._emit("espdev", what="input", device=did, point=pid, value=sent, log=False)
        except ModelError as e:
            self._failed[(did, pid)] = time.monotonic()
            self.last.setdefault(did, {})[pid] = f"{label}: niet verstuurd ({e}), opnieuw over {int(RETRY)} s"
        finally:
            self._inflight.discard((did, pid))
            self._poke()

    # ------------------------------------------------------------ voor de pagina: nooit het token

    def _point_view(self, did: str, dev: dict, p: dict) -> dict:
        e = self.values.get(did, {}).get(p["id"])
        v = self._value_of(did, p)
        label = next((o["l"] for o in p["options"] if o["v"] == v), None) if p["kind"] == "enum" and v is not None else None
        return {**p, "value": v, "value_label": label, "age": int(e["age"] + (time.monotonic() - e["at"]) * 1000) if e and v is not None else None,
                "gone": bool(p.get("gone")), "ref": f"e:{did}:{p['id']}", "input": dev["inputs"].get(p["id"])}

    def status(self) -> list[dict]:
        out = []
        for did, dev in self.devices.items():
            pts = [self._point_view(did, dev, p) for p in dev["points"]]
            out.append({"id": did, "hid": dev["hid"], "name": dev["name"], "host": self.bridges[dev["bridge"]]["host"] if dev["driver"] == "hue" and dev["bridge"] in self.bridges
                        else "api.nuki.io" if dev["driver"] == "nuki" else "www.brink-home.com" if dev["driver"] == "brink" else "dashboard.smappee.net" if dev["driver"] == "smappee" else dev["host"], "bridge": dev["bridge"], "account": dev["account"], "port": dev["port"], "type": dev["type"], "fw": dev["fw"], "proto": dev["proto"],
                        "proto_state": self.proto_state_of(dev), "current": f"{CURRENT[0]}.{CURRENT[1]}", "rev": dev["rev"], "online": did in self.online, "error": self.errors.get(did, ""),
                        "configured": True, "driver": dev["driver"], "ha": dev["ha"], "skipped": list(dev["skipped"]), "shown": self.shown(dev), "points": pts,
                        "actions": [{**a, "disabled": a["id"] in self.off.get(did, {}), "reason": self.off.get(did, {}).get(a["id"], "")} for a in dev["actions"]],
                        "inputs": dict(dev["inputs"]), "last": dict(self.last.get(did, {}))})
        return out

    def found_view(self) -> list[dict]:
        """Gevonden en nog niet van ons. Een apparaat dat al door iets anders geclaimd is, staat er ook, maar is niet te koppelen."""
        now = time.time()
        mine = {dev["hid"] for dev in self.devices.values()}
        return [{"hid": k, "type": v["type"], "name": v["name"] or k, "ip": v["ip"], "port": v["port"], "fw": v["fw"], "proto": v["proto"], "proto_state": proto_state(v["proto"]),
                 "claimed": v["claimed"], "claimable": not v["claimed"] and proto_state(v["proto"]) != "unsupported"}
                for k, v in sorted(self.found.items(), key=lambda kv: kv[1]["ip"]) if k not in mine and now - v["seen"] < FOUND_KEEP]

    # ------------------------------------------------------------ MQTT

    @staticmethod
    def _oid(did: str, key: str) -> str:
        return f"kk_esp_{did}_{key}"

    @staticmethod
    def t(did: str, what: str, kind: str = "state") -> str:
        return f"{d.PREFIX}/esp/{did}/{what}/{kind}"

    @property
    def filters(self) -> list[str]:
        return [f"{d.PREFIX}/esp/+/+/set", f"{d.PREFIX}/esp/+/do/+/press"]

    def _entity(self, did: str, p: dict) -> tuple[str, dict]:
        """(domein, specifieke velden) voor een punt: zoals bij Tuya gekozen op soort en schrijfbaarheid. Gevaarlijke punten zijn alleen te lezen."""
        rw = p["writable"] and not p["danger"]
        out: dict = {}
        k = p["kind"]
        if k == "bool":
            domain = "switch" if rw else "binary_sensor"
            out.update(payload_on="ON", payload_off="OFF")
        elif k == "enum" and rw:
            domain = "select"
            out["options"] = [o["l"] for o in p["options"]]
        elif k in ("int", "float") and rw and p["min"] is not None and p["max"] is not None:
            domain = "number"
            out.update(min=p["min"], max=p["max"], step=p["step"] or (1 if k == "int" else 10 ** -p["decimals"]), mode="box")
        else:
            domain = "sensor"
        if rw and domain in ("switch", "select", "number"):
            out["command_topic"] = self.t(did, p["id"], "set")
        if p["unit"] and k in ("int", "float"):
            if p["cls"] != "ph":
                out["unit_of_measurement"] = p["unit"]
        if p["cls"] in HA_CLASSES and k in ("int", "float") and domain in ("sensor", "number"):
            out["device_class"] = p["cls"]
        if domain == "sensor" and k in ("int", "float"):
            if p["state"]:
                out["state_class"] = p["state"]
            if k == "float":
                out["suggested_display_precision"] = p["decimals"]
        if domain == "sensor" and k == "enum":
            out.update(device_class="enum", options=[o["l"] for o in p["options"]])
        if p["diag"]:
            out["entity_category"] = "diagnostic"
        return domain, out

    def discovery(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for did, dev in self.devices.items():
            if not dev["ha"]:
                continue
            drv = dev["driver"]
            device = {"identifiers": [f"kk_esp_{did}"], "name": dev["name"], "manufacturer": {"esphome": "ESPHome", "hue": "Signify", "nuki": "Nuki", "brink": "Brink", "smappee": "Smappee"}.get(drv, "Zelfbouw"),
                      "model": {"esphome": "ESPHome (webserver)", "hue": dev["type"] or "Hue", "nuki": "Smart Lock", "brink": "Ventilatie-unit", "smappee": "Laadpaal (EV Wall)"}.get(drv) or f"{dev['type'] or 'apparaat'} (HUISDEV)", "sw_version": dev["fw"] or None}
            device = {k: v for k, v in device.items() if v}
            for p in dev["points"]:
                domain, extra = self._entity(did, p)
                oid = self._oid(did, p["id"])
                out[f"{d.DISCOVERY}/{domain}/{oid}/config"] = {
                    "device": device, "has_entity_name": True, "name": p["label"], "unique_id": oid, "default_entity_id": f"{domain}.{oid}", "state_topic": self.t(did, p["id"]),
                    "availability_mode": "all", "availability": [{"topic": d.BRIDGE_STATUS}, {"topic": self.t(did, "online")}, {"topic": self.t(did, p["id"], "avail")}],
                    "payload_available": "online", "payload_not_available": "offline", **extra}
            for a in dev["actions"]:  # een knop per actie zonder argumenten die niet gevaarlijk is
                if a["args"] or a["danger"]:
                    continue
                oid = self._oid(did, "do_" + a["id"])
                out[f"{d.DISCOVERY}/button/{oid}/config"] = {
                    "device": device, "has_entity_name": True, "name": a["label"], "unique_id": oid, "default_entity_id": f"button.{oid}",
                    "command_topic": f"{d.PREFIX}/esp/{did}/do/{a['id']}/press", "payload_press": "PRESS",
                    "availability_mode": "all", "availability": [{"topic": d.BRIDGE_STATUS}, {"topic": self.t(did, "online")}], "payload_available": "online", "payload_not_available": "offline"}
        return out

    def states(self) -> dict[str, tuple[str, bool]]:
        out: dict[str, tuple[str, bool]] = {}
        for did, dev in self.devices.items():
            if not dev["ha"]:
                continue
            out[self.t(did, "online")] = ("online" if did in self.online else "offline", True)
            for p in dev["points"]:
                out[self.t(did, p["id"], "avail")] = ("offline" if p.get("gone") else "online", True)
                v = self._value_of(did, p)
                if v is None:
                    continue
                k = p["kind"]
                text = ("ON" if v else "OFF") if k == "bool" else next((o["l"] for o in p["options"] if o["v"] == v), str(v)) if k == "enum" else \
                    f"{v:.{p['decimals']}f}" if k == "float" else str(v)
                out[self.t(did, p["id"])] = (text, True)
        return out

    def command(self, topic: str, payload: str) -> bool:
        m = SET_RE.match(topic)
        press = PRESS_RE.match(topic)
        if not m and not press:
            return False
        asyncio.get_running_loop().create_task(self._mqtt(m, press, payload))
        return True

    async def _mqtt(self, m, press, payload: str) -> None:
        try:
            if press:
                did, aid = press.groups()
                a = next((x for x in self.devices.get(did, {"actions": []})["actions"] if x["id"] == aid), None)
                if not a or a["args"] or a["danger"]:
                    raise ModelError("deze actie is niet via HA te starten")
                await self.do(did, aid)
                return
            did, pid = m.groups()
            dev = self.devices.get(did)
            p = self._by_id(dev, pid) if dev else None
            if not p:
                raise ModelError("onbekend punt")
            if p["danger"]:
                raise ModelError("een gevaarlijk punt stel je alleen in vanuit de zijbalk")
            value = payload
            if p["kind"] == "enum":
                hit = [o["v"] for o in p["options"] if o["l"] == payload]
                if not hit:
                    raise ModelError(f"onbekende keuze {payload!r}")
                value = hit[0]
            await self.set(did, pid, value)
        except ModelError as e:
            log.warning("HA-opdracht %s: %s", (m or press).group(0), e)


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, cb) -> None:
        self.cb = cb

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self.cb(data, addr)
        except Exception:  # noqa: BLE001 - een rare datagram mag het luisteren niet stoppen
            log.exception("HUISDEV-datagram")
