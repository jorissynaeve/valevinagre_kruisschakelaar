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
from . import model as _model
from .engine import Engine
from .logic import BOOL, NUMBER
from .model import ModelError, fits, source_type

log = logging.getLogger("kk.espdev")

UDP_PORT = 47500
CURRENT = (1, 0)  # het protocol zoals de add-on het kent (protocol/huisdev/HUISDEV.md); een lagere minor van een apparaat toont "upgrade beschikbaar"
SUPPORTED_MAJORS = (1,)
MAX_DEVICES = 16
MAX_POINTS, MAX_ACTIONS, MAX_ARGS = 64, 16, 8
POLL = 3.0  # s
FAILS = 3  # zo vaak achter elkaar mislukt = offline
SCAN_EVERY = 60  # s: zolang een apparaat offline is of er niets gevonden is
FOUND_KEEP = 3600  # s: wat zich zo lang niet meer meldde, verdwijnt uit de keuzelijst
GONE_KEEP = 30 * 86400  # s: een verdwenen punt blijft zo lang "niet beschikbaar"
RETRY = 30.0  # s: een ingang die niet doorkwam, opnieuw proberen
TICK = 1.0
DEFAULT_SHOWN = 8

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
                   cls=_text(raw.get("class"), 30), state=raw.get("state") if raw.get("state") in ("measurement", "total_increasing") else "",
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
        self.last: dict[str, dict] = {}  # did -> {punt: wat er laatst gebeurde}, voor de zijbalk
        self._listen = None
        self._ask = None
        self._http: aiohttp.ClientSession | None = None
        self.udp_port = port or UDP_PORT
        self._load()
        engine.espdev_view = self.status
        engine.espdev_found = self.found_view
        engine.espdev_value = self.output
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
        for k, v in (raw.get("devices") or {}).items():
            if isinstance(v, dict) and re.fullmatch(r"[a-z0-9_]{1,40}", k) and isinstance(v.get("hid"), str) and ID_RE.match(v["hid"]):
                self.devices[k] = self._defaults(v)
        if isinstance(raw.get("retired"), dict):
            self.retired = {t: float(ts) for t, ts in raw["retired"].items() if isinstance(t, str) and isinstance(ts, (int, float))}

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
        return {"name": _text(v.get("name"), 60) or "Apparaat", "hid": v["hid"], "host": _text(v.get("host"), 253), "port": port if isinstance(port, int) and 1 <= port <= 65535 else 80,
                "type": _text(v.get("type"), 40), "fw": _text(v.get("fw"), 40), "proto": v.get("proto") if parse_proto(v.get("proto")) else "1.0", "rev": _text(v.get("rev"), 16),
                "token": _text(v.get("token"), 128), "points": pts, "actions": acts, "shown": [x for x in shown if isinstance(x, str)] if isinstance(shown, list) else None,
                "inputs": {k: r for k, r in ins.items() if isinstance(k, str) and isinstance(r, str)}}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"devices": self.devices, "retired": self.retired}, f, ensure_ascii=False, indent=1)
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
        if "host" in b:
            host = _text(b["host"], 253)
            if not HOST_RE.match(host):
                raise ModelError("ongeldig adres: geef het IP zonder http://")
            new["host"] = host
        if new["inputs"] != dev["inputs"]:
            self.seen.pop(did, None)  # een andere bron: haar eerste stand wordt weer alleen onthouden
        self.devices[did] = new
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
            p = self._by_id(dev, pid) if isinstance(pid, str) else None
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
            status, raw = await self._call(tmp, "GET", "/huisdev/descriptor", token=False)
            desc = clean_descriptor(raw) if status == 200 else None
            if not desc or desc["id"] != hid:
                raise ModelError("het apparaat geeft geen geldige beschrijving (HUISDEV)")
            if proto_state(desc["proto"]) == "unsupported":
                raise ModelError(f"protocol {desc['proto']} wordt niet ondersteund")
            token = secrets.token_hex(32)
            status, raw = await self._call(tmp, "POST", "/huisdev/claim", {"token": token}, token=False)
        except EspError as e:
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

    async def remove(self, did: str, unclaim: bool = True) -> bool:
        dev = self.devices.get(did)
        if not dev:
            return False
        self._stop(did)
        if unclaim and dev["token"]:  # het apparaat meteen vrijgeven; lukt dat niet, dan wist u de claim op het apparaat zelf
            try:
                await self._call(dev, "POST", "/huisdev/unclaim", {})
            except EspError as e:
                log.warning("%s: claim intrekken mislukt (%s); wis hem op het apparaat", dev["name"], e)
        for p in dev["points"]:
            self._retire(did, dev, p["id"])
        for a in dev["actions"]:
            self._retire(did, dev, action=a["id"])
        del self.devices[did]
        for x in (self.values, self.off, self.seen, self.last, self.fails, self.errors):
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
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3))
        return self._http

    async def _call(self, dev: dict, method: str, path: str, body=None, token: bool = True) -> tuple[int, dict]:
        url = f"http://{dev['host']}:{dev['port']}{path}"
        headers = {"X-Huisdev-Token": dev["token"]} if token and dev.get("token") else {}
        try:
            async with self._session().request(method, url, json=body, headers=headers) as r:
                raw = await r.content.read(65536)
                status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise EspError(0, "net", f"geen verbinding met {dev['host']}: {e or 'time-out'}") from None
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        return status, data if isinstance(data, dict) else {}

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        for did in self.devices:
            self._start(did)
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
        if did in self.tasks:
            return
        self.wake[did] = asyncio.Event()
        self.tasks[did] = asyncio.get_running_loop().create_task(self._run(did))

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
        http, self._http = self._http, None
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

    async def _refetch(self, did: str) -> None:
        dev = self.devices[did]
        status, raw = await self._call(dev, "GET", "/huisdev/descriptor", token=False)
        desc = clean_descriptor(raw) if status == 200 else None
        if not desc:
            raise EspError(status, "descriptor", "het apparaat geeft geen geldige beschrijving")
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
            out.append({"id": did, "hid": dev["hid"], "name": dev["name"], "host": dev["host"], "port": dev["port"], "type": dev["type"], "fw": dev["fw"], "proto": dev["proto"],
                        "proto_state": self.proto_state_of(dev), "current": f"{CURRENT[0]}.{CURRENT[1]}", "rev": dev["rev"], "online": did in self.online, "error": self.errors.get(did, ""),
                        "configured": True, "shown": self.shown(dev), "points": pts,
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
            device = {"identifiers": [f"kk_esp_{did}"], "name": dev["name"], "manufacturer": "Zelfbouw", "model": f"{dev['type'] or 'apparaat'} (HUISDEV)", "sw_version": dev["fw"] or None}
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
