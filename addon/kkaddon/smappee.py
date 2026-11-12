"""Smappee-laadpalen (EV Wall) via de cloud van Smappee als HUISDEV-apparaat: een laadpunt (connector) is een blok `e:<id>`.

Smappee heeft geen openbare API voor de EV Wall en geen lokale. Dit volgt de calls van het eigen dashboard (dashboard.smappee.net), zoals de Home Assistant-integratie
`myny-git/smappee_ev` ze ook gebruikt (MIT). Het is dus niet door Smappee gedocumenteerd en kan zonder waarschuwing veranderen. Alleen wat daar bewezen is, is hier overgenomen.

  inloggen  `POST /dashapi/login` {userName, password} -> {token, refreshToken, tokenExpirationTimestamp (ms)}; daarna de header `token: <token>`
  lezen     `GET /api/v11/user/servicelocations?fullDetails=true`            de locaties, met `chargingStation.serialNumber` (de locatie met het laadstation is de "control"-locatie)
            `GET /api/v10/chargingstations/<serial>?includeDetails=true`     `available`, en `modules[]` met per laadpunt `position` en `smartDevice` (`id`, `uuid`, `name`, categorie CARCHARGER)
            `GET /api/v10/servicelocation/<loc>/homecontrol/smart/devices`   per laadpunt `properties` (`chargingState`, `percentageLimit`), `configurationProperties` (min/max stroom)
            `GET /api/v10/chargingstations/<serial>/sessions?range=<van>,<tot>&rangeMode=stop_or_start`   sessies: `from`, `to`, `energy` (kWh), `rfidToken`
  schrijven `POST .../smart/devices/<id>/actions/pauseCharging` met `[]`                    pauzeren
            `POST .../smart/devices/<id>/actions/setChargingMode` met `mode: STANDARD`      hervatten (de knop Standaard van de app, ook als je gepauzeerd bent)
            `POST .../smart/devices/<id>/actions/setPercentageLimit` met `percentageLimit`  de stroom, als percentage van het bereik min..max van het laadpunt

Starten en stoppen zijn hier **niet** gebouwd: met een badge (RFID) start de eigenaar van de tag een sessie aan de laadpaal, niet de huisconfig.

De stroom is bij Smappee een percentage; een heel getal ampère maakt daar het dichtstbijzijnde percentage van, en we lezen het terug afgerond op een heel getal. Zolang het bereik kleiner is dan 100 A
komt dat altijd op dezelfde ampère uit. Staat de lader op Slim of Zon, dan negeert hij die limiet: het punt is dan onbekend en schrijven wordt geweigerd.

Een punt is alleen bekend als de lader het zelf meldt; een onbekende status is onbekend, nooit een gok.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

import aiohttp

BASE = "https://dashboard.smappee.net"
DASHAPI = "/dashapi"
API = "/api"
TIMEOUT = 20
MAX_BODY = 4 << 20
SESSION_DAYS = 7  # zover terug vragen we sessies (zoals de HA-integratie)
SERIAL_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ID_RE = re.compile(r"^[0-9]{1,20}$")
DEFAULT_MIN, DEFAULT_MAX = 6, 32  # A, als de lader zijn bereik niet meldt (zoals de HA-integratie)

STATES = {0: "Vrij", 1: "Aangesloten", 2: "Laadt", 3: "Gepauzeerd"}
S_FREE, S_CONNECTED, S_CHARGING, S_PAUSED = 0, 1, 2, 3

CAT_CHARGER = "CARCHARGER"
PROP_MIN = "etc.smart.device.type.car.charger.config.min.current"
PROP_MAX = "etc.smart.device.type.car.charger.config.max.current"

ACTIVE_WORDS = {"STARTED", "CHARGING", "CHARGING_STARTED", "RUNNING"}
PAUSED_WORDS = {"SUSPENDED", "PAUSED"}
FINISHED_WORDS = {"STOPPED", "CHARGING_FINISHED", "FINISHED", "COMPLETED", "IDLE"}


class SmappeeError(Exception):
    """Een fout bij Smappee, met een zin die zegt wat er mis is. `code`: `auth` (e-mail of wachtwoord onjuist), `token` (de sessie is verlopen), `rate` (te veel verzoeken, `wait` in seconden),
    `maintenance`, `forbidden` of `net`."""

    def __init__(self, code: str, reason: str, status: int = 0, wait: float = 0.0) -> None:
        super().__init__(reason)
        self.code, self.reason, self.status, self.wait = code, reason, status, wait


# ------------------------------------------------------------ kleine hulpjes

def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == v and abs(v - round(v)) < 1e-9:
        return int(round(v))
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v == v:
        return float(v)
    if isinstance(v, str):
        try:
            x = float(v.strip())
        except ValueError:
            return None
        return x if x == x else None
    return None


def _text(v: Any, n: int = 60) -> str:
    return v.strip()[:n] if isinstance(v, str) else ""


def unwrap(raw: Any) -> Any:
    """De waarde in een eigenschap van Smappee: een gewone waarde, of een getypeerd omhulsel (`{"Integer": 5}`, `{"Quantity": {"value": 6, "unit": "A"}}`, `{"String": "x"}`)."""
    if not isinstance(raw, dict):
        return raw
    q = raw.get("Quantity")
    if isinstance(q, dict):
        return q.get("value")
    for k in ("Integer", "String", "Double", "Boolean", "value"):
        if k in raw:
            return raw[k]
    return None


def prop_value(prop: Any) -> Any:
    """De waarde van een eigenschap (`value`, of de eerste van `values`)."""
    if not isinstance(prop, dict):
        return None
    if "value" in prop:
        return unwrap(prop["value"])
    vals = prop.get("values")
    return unwrap(vals[0]) if isinstance(vals, list) and vals else None


def prop_named(props: Any, name: str) -> Any:
    for p in props if isinstance(props, list) else []:
        spec = p.get("spec") if isinstance(p, dict) else None
        if isinstance(spec, dict) and spec.get("name") == name:
            return prop_value(p)
    return None


def account_id(username: str) -> str:
    return "s_" + hashlib.sha256(username.strip().lower().encode()).hexdigest()[:6]


def hid_of(serial: str, device_id: str) -> str:
    """Het HUISDEV-id van een laadpunt: kleine letters, cijfers en streepjes, hoogstens 32 tekens."""
    raw = f"smappee-{serial.lower()}-{device_id}"
    s = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    return s if len(s) <= 32 else f"smappee-{hashlib.sha1(raw.encode()).hexdigest()[:24]}"


# ------------------------------------------------------------ de lijst lezen

def stations(locations: Any) -> dict[str, dict]:
    """`GET servicelocations?fullDetails=true` als {serienummer: {name, loc}}. `loc` is de locatie waar het laadstation hangt: daar horen de smart devices en de acties bij."""
    out: dict[str, dict] = {}
    for loc in locations if isinstance(locations, list) else []:
        if not isinstance(loc, dict):
            continue
        lid = _int(loc.get("id", loc.get("serviceLocationId", loc.get("locationId"))))
        st = loc.get("chargingStation") or loc.get("chargingstation")
        if not isinstance(st, dict):
            sts = loc.get("chargingStations") or loc.get("chargingstations")
            st = next((x for x in sts if isinstance(x, dict)), None) if isinstance(sts, list) else None
        serial = _text(st.get("serialNumber") or st.get("serial"), 40) if isinstance(st, dict) else ""
        if lid is not None and lid > 0 and SERIAL_RE.match(serial):
            out[serial] = {"name": _text(loc.get("name")) or f"Smappee {serial}", "loc": str(lid)}
    return out


def connectors(details: Any) -> list[dict]:
    """`GET chargingstations/<serial>?includeDetails=true` als de lijst laadpunten: {id, uuid, name, pos}. Een module die geen laadpunt is (de led) telt niet mee."""
    out = []
    for m in details.get("modules") if isinstance(details, dict) and isinstance(details.get("modules"), list) else []:
        sd = m.get("smartDevice") if isinstance(m, dict) else None
        if not isinstance(sd, dict):
            continue
        t = sd.get("type")
        cat = t.get("category") if isinstance(t, dict) else t
        did = _int(sd.get("id"))
        if str(cat or "").upper() != CAT_CHARGER or did is None or did <= 0:
            continue
        pos = _int(m.get("position"))
        out.append({"id": str(did), "uuid": _text(sd.get("uuid"), 80), "name": _text(sd.get("name")), "pos": pos if pos is not None else len(out) + 1})
    return out


def device_map(devices: Any) -> dict[str, dict]:
    """`GET smart/devices` als {sleutel: apparaat}, met elk apparaat onder zijn `id`, `uuid`, `smartDeviceId` en `smartDeviceUuid` (de HA-integratie zoekt op al die vier). Er wordt **niet** op categorie
    gefilterd: welke apparaten laadpunten zijn, zegt het laadstation (`connectors`); de lijst hoeft zijn categorie niet te geven."""
    out = {}
    for d in devices if isinstance(devices, list) else []:
        if not isinstance(d, dict):
            continue
        for k in ("id", "uuid", "smartDeviceId", "smartDeviceUuid"):
            v = d.get(k)
            key = str(_int(v)) if _int(v) is not None else v.strip() if isinstance(v, str) and v.strip() else None
            if key:
                out.setdefault(key, d)
    return out


def categories(devices: Any) -> list[str]:
    """De categorieën die de lijst met smart devices noemt, voor een foutmelding die zegt wat Smappee wel gaf."""
    out = set()
    for d in devices if isinstance(devices, list) else []:
        t = d.get("type") if isinstance(d, dict) else None
        c = t.get("category") if isinstance(t, dict) else t
        out.add(str(c)[:30] if c else "zonder categorie")
    return sorted(out)[:8]


# ------------------------------------------------------------ van een laadpunt naar punten

def _pt(pid: str, label: str, kind: str, **kw) -> dict:
    opts = kw.pop("options", None)
    return {"id": pid, "label": label, "kind": kind, "unit": "", "decimals": 2 if kind == "float" else 0, "min": None, "max": None, "step": None,
            "options": [{"v": k, "l": v} for k, v in opts.items()] if opts else [], "writable": False, "danger": False, "diag": False, "group": "", "cls": "", "state": "", "stale": 0,
            "confirm": "", **kw}


def phase(dev: dict) -> str | None:
    """`active`, `paused`, `finished` of None volgens de eigen woorden van de lader (zoals de HA-integratie: eerst de laadstand, dan de status)."""
    car = dev.get("carCharger") if isinstance(dev.get("carCharger"), dict) else {}
    status = car.get("status") if isinstance(car.get("status"), dict) else {}
    for raw in (prop_named(dev.get("properties"), "chargingState"), status.get("current")):
        w = str(raw or "").strip().upper()
        if w in FINISHED_WORDS:
            return "finished"
        if w in ACTIVE_WORDS:
            return "active"
        if w in PAUSED_WORDS or w.startswith("SUSPENDED_EVSE"):
            return "paused"
    return None


def connected(dev: dict) -> bool | None:
    """Staat er een auto aan? `connectionStatus` (CONNECTED/DISCONNECTED) of de IEC-stand (A geen auto, B aangesloten, C laadt); anders onbekend."""
    car = dev.get("carCharger") if isinstance(dev.get("carCharger"), dict) else {}
    cs = str(car.get("connectionStatus") or "").strip().upper()
    if cs in ("CONNECTED", "DISCONNECTED"):
        return cs == "CONNECTED"
    iec = str(car.get("iecStatus") or "").strip().upper()
    if iec[:1] in ("B", "C"):
        return True
    if iec[:1] == "A":
        return False
    return None


def raw_paused(dev: dict) -> bool:
    car = dev.get("carCharger") if isinstance(dev.get("carCharger"), dict) else {}
    return str(car.get("chargingMode") or "").strip().upper() == "PAUSED"


def state_of(dev: dict) -> int | None:
    """De status van het laadpunt (`STATES`), of None als de lader niets zegt waar we iets mee kunnen."""
    ph = phase(dev)
    if ph == "active":
        return S_CHARGING
    if ph == "paused" or (ph is None and raw_paused(dev)):
        return S_PAUSED
    if ph in ("finished", None):
        c = connected(dev)
        return None if c is None else S_CONNECTED if c else S_FREE
    return None


def current_range(dev: dict) -> tuple[int, int]:
    """(min, max) ampère van het laadpunt. Een onbruikbaar bereik geeft de standaard (6 tot 32), zoals de HA-integratie."""
    props = dev.get("configurationProperties")
    lo, hi = _int(prop_named(props, PROP_MIN)), _int(prop_named(props, PROP_MAX))
    lo = DEFAULT_MIN if lo is None else lo
    hi = DEFAULT_MAX if hi is None else hi
    return (lo, hi) if 0 <= lo <= hi and hi > 0 else (DEFAULT_MIN, DEFAULT_MAX)


def strategy(dev: dict) -> str:
    """De optimalisatiestrategie (NONE, EXCESS_ONLY, SCHEDULES_FIRST_THEN_EXCESS), of "" als de lijst hem niet geeft."""
    car = dev.get("carCharger") if isinstance(dev.get("carCharger"), dict) else {}
    return str(car.get("optimizationStrategy") or "").strip().upper()


def limit_applies(dev: dict) -> bool:
    """Bepaalt de stroomlimiet wat de lader doet? Alleen zonder optimalisatie (Standaard); bij Slim of Zon beslist de lader zelf. Geeft de lijst geen strategie, dan gaan we ervan uit van wel."""
    return strategy(dev) in ("", "NONE")


def percentage_of(dev: dict) -> int | None:
    pct = _num(prop_named(dev.get("properties"), "percentageLimit"))
    return int(round(pct)) if pct is not None and 0 <= pct <= 100 else None


def current_of(dev: dict) -> int | None:
    """De ingestelde stroom in hele ampère, of None (onbekend, of de lader staat op Slim of Zon en negeert de limiet)."""
    pct = percentage_of(dev)
    if pct is None or not limit_applies(dev):
        return None
    lo, hi = current_range(dev)
    return int(round(lo + pct / 100.0 * (hi - lo))) if hi > lo else lo


def percentage_for(amps: float, lo: int, hi: int) -> int:
    if hi <= lo:
        return 100
    return max(0, min(100, int(round((float(amps) - lo) * 100.0 / (hi - lo)))))


def describe(unit: dict, name: str = "") -> dict:
    """De descriptor van een laadpunt in de vorm van `espdevhub.clean_descriptor` (zonder device-velden). `unit` is {dev, station_available, ...} uit de laatste ronde."""
    lo, hi = current_range(unit["dev"])
    pts = [
        _pt("state", "Status", "enum", options=STATES),
        _pt("charging", "Laadt", "bool"),
        _pt("paused", "Gepauzeerd", "bool", writable=True),
        _pt("current", "Stroom", "int", unit="A", min=lo, max=hi, step=1, writable=True),
        _pt("session_kwh", "Laatste sessie", "float", unit="kWh", cls="energy", decimals=2),
        _pt("available", "Beschikbaar", "bool", diag=True),
    ]
    body = json.dumps(pts, sort_keys=True, separators=(",", ":"))
    return {"proto": "1.0", "rev": hashlib.sha1(body.encode()).hexdigest()[:8], "type": "Laadpaal", "name": name or unit.get("name") or "Laadpaal", "fw": "", "lib": "",
            "points": pts, "actions": []}


def values(unit: dict, session_kwh: float | None = None) -> dict[str, Any]:
    """De waarde per punt, of None. `session_kwh` is de energie van de laatste sessie van dit laadpunt uit de sessielijst."""
    dev = unit["dev"]
    st = state_of(dev)
    pz = None if st is None else st == S_PAUSED
    return {"state": st, "charging": None if st is None else st == S_CHARGING, "paused": pz, "current": current_of(dev), "session_kwh": session_kwh,
            "available": unit.get("station_available") if isinstance(unit.get("station_available"), bool) else None}


# ------------------------------------------------------------ opdrachten

def _action(unit: dict, name: str, body: list) -> tuple[str, str, list]:
    return "POST", f"{API}/v10/servicelocation/{unit['loc']}/homecontrol/smart/devices/{unit['id']}/actions/{name}", body


def write_request(unit: dict, pid: str, value) -> tuple[str, str, Any] | None:
    """(methode, pad, body) voor een instelbaar punt, of None als er niets te doen is (al gepauzeerd, of al niet gepauzeerd: dan sturen we niets, want hervatten zet de lader op Standaard).
    Pauzeren mag ook bij een onbekende status; hervatten niet. ValueError met de reden als het niet kan."""
    dev = unit["dev"]
    if pid == "paused":
        st = state_of(dev)
        now = None if st is None else st == S_PAUSED
        if bool(value) == now:
            return None
        if value:
            return _action(unit, "pauseCharging", [])
        if now is None:
            raise ValueError("de status van de lader is onbekend: hervatten zet hem op Standaard, dat doe ik niet blind")
        return _action(unit, "setChargingMode", [{"spec": {"name": "mode", "species": "String", "required": True,
                                                          "possibleValues": {"values": [{"String": "STANDARD"}, {"String": "SMART"}, {"String": "SOLAR"}], "exhaustive": True}},
                                                  "values": [{"String": "STANDARD"}]}])
    if pid == "current":
        if not limit_applies(dev):
            raise ValueError("de lader staat op Slim of Zon en negeert een stroomlimiet: zet hem in de app op Standaard")
        lo, hi = current_range(dev)
        a = _num(value)
        if a is None or a < lo - 1e-9 or a > hi + 1e-9:
            raise ValueError(f"de stroom moet tussen {lo} en {hi} A liggen")
        return _action(unit, "setPercentageLimit", [{"spec": {"name": "percentageLimit", "species": "Integer", "unit": "%", "required": True},
                                                      "values": [{"Integer": percentage_for(a, lo, hi)}]}])
    raise ValueError("dit punt is niet instelbaar")


# ------------------------------------------------------------ sessies

def _seconds(v: Any) -> float | None:
    x = _num(v)
    if x is None or x <= 0:
        return None
    return x / 1000.0 if x > 10_000_000_000 else x


def _nested(d: dict, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        cur: Any = d
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                cur = None
                break
            cur = cur[k]
        if cur not in (None, ""):
            return cur
    return None


def _for_connector(s: dict, unit: dict, only: bool) -> bool:
    """Hoort deze sessie bij dit laadpunt? Heeft het laadstation er maar een, dan altijd. Anders op het id, de uuid of het nummer van het laadpunt; wat we niet kunnen plaatsen, blijft weg (nooit een gok)."""
    if only:
        return True
    u = _nested(s, ("connectorUuid",), ("smartDeviceUuid",), ("deviceUuid",), ("connector", "uuid"), ("smartDevice", "uuid"))
    if u is not None and unit.get("uuid") and str(u) == unit["uuid"]:
        return True
    i = _nested(s, ("connectorId",), ("smartDeviceId",), ("deviceId",), ("connector", "id"), ("smartDevice", "id"))
    if i is not None and str(i) == unit["id"]:
        return True
    n = _nested(s, ("connectorNumber",), ("connector", "number"), ("connector", "position"), ("position",))
    return n is not None and str(n) == str(unit.get("pos"))


def sessions(data: Any, unit: dict, only: bool) -> list[dict]:
    """De sessies van een laadpunt uit `GET chargingstations/<serial>/sessions`, de nieuwste eerst: {key, start, end (None = loopt), kwh, tag}.
    `key` is het begin in seconden: dat is wat een sessie bij herhaald ophalen herkenbaar maakt. Een sessie zonder begin telt niet."""
    out = []
    for s in data if isinstance(data, list) else []:
        if not isinstance(s, dict) or not _for_connector(s, unit, only):
            continue
        start = _seconds(s.get("from"))
        if start is None:
            continue
        end = _seconds(s.get("to"))
        kwh = _num(s.get("energy"))
        tag = s.get("rfidToken")
        out.append({"key": str(int(start)), "start": start, "end": end if end is not None and end >= start else None, "kwh": kwh if kwh is not None and kwh >= 0 else None,
                    "tag": tag.strip()[:80] if isinstance(tag, str) and tag.strip() else ""})
    out.sort(key=lambda x: -x["start"])
    return out


# ------------------------------------------------------------ inloggen en verzoeken

async def _read(r: aiohttp.ClientResponse) -> bytes:
    parts, size = [], 0
    async for chunk in r.content.iter_chunked(16384):  # de hele body lezen (zie hue.py)
        size += len(chunk)
        if size > MAX_BODY:
            raise SmappeeError("net", "antwoord van Smappee te groot")
        parts.append(chunk)
    return b"".join(parts)


def _retry_after(r: aiohttp.ClientResponse) -> float:
    v = r.headers.get("Retry-After", "")
    return float(v) if v.strip().isdigit() else 120.0


def _classify(status: int, raw: bytes, r: aiohttp.ClientResponse) -> None:
    if b"under maintenance" in raw.lower()[:4096]:
        raise SmappeeError("maintenance", "Smappee is in onderhoud", status)
    if status == 429:
        w = _retry_after(r)
        raise SmappeeError("rate", f"te veel verzoeken bij Smappee: {int(w)} s wachten", status, w)
    if status == 408 or status >= 500:
        raise SmappeeError("net", f"Smappee antwoordt met HTTP {status}", status)


async def login(session: aiohttp.ClientSession, username: str, password: str) -> tuple[str, float]:
    """Inloggen met het wachtwoord van het dashboard. Geeft (token, geldig in seconden). Een onjuist wachtwoord is SmappeeError("auth"): probeer het dan niet steeds opnieuw."""
    try:
        async with session.post(f"{BASE}{DASHAPI}/login", json={"userName": username, "password": password}, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            raw = await _read(r)
            status = r.status
            _classify(status, raw, r)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise SmappeeError("net", f"geen verbinding met Smappee: {e or 'time-out'}") from None
    if status in (401, 403):
        raise SmappeeError("auth", "Smappee weigert de gebruikersnaam of het wachtwoord", status)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = {}
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"].strip():
        raise SmappeeError("net", f"Smappee geeft geen toegangstoken (HTTP {status})", status)
    exp = _num(data.get("tokenExpirationTimestamp"))
    ttl = (exp / 1000.0 - time.time()) if exp is not None else 3600.0
    return data["token"], ttl if ttl > 60 else 3600.0


async def request(session: aiohttp.ClientSession, token: str, method: str, path: str, body=None, params: dict | None = None, timeout: float = TIMEOUT) -> tuple[int, Any]:
    """Een verzoek aan de API. Geeft (status, JSON). Een verlopen sessie (401), een geweigerd recht, te veel verzoeken en onderhoud zijn een SmappeeError."""
    try:
        async with session.request(method, BASE + path, json=body, params=params, headers={"token": token, "content-type": "application/json"},
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            raw = await _read(r)
            status = r.status
            _classify(status, raw, r)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise SmappeeError("net", f"geen verbinding met Smappee: {e or 'time-out'}") from None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    if status == 401:
        raise SmappeeError("token", "de sessie bij Smappee is verlopen", status)
    if status == 403:
        raise SmappeeError("forbidden", "Smappee weigert dit voor dit account (geen schrijfrecht op dit laadstation?)", status)
    return status, data
