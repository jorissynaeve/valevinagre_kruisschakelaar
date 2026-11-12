"""Laadpalen met laadsessies, als eigen blok `k:<id>`.

Een laadpaal hangt aan één Tuya-toestel van deze installatie (de eigenaar). Andere installaties (een ander subnet met een eigen
HA en add-on) bedienen hem niet meer via de ruwe datapunten maar via dit blok, zodat de eigenaar weet wie wanneer laadt:

  * **Eén sessie tegelijk.** Wie start (deze installatie, of een andere via de koppeling) wordt de starter. Een start terwijl er
    een sessie loopt wordt geweigerd. Alleen de starter mag stoppen, en de eigenaar altijd (als noodknop).
  * **Verbergen.** Tijdens een sessie krijgt een andere installatie niets dan "bezet": geen status, geen kWh, geen naam van de
    starter. Dat gebeurt aan de kant van de eigenaar (`view`): de gegevens gaan nooit over de lijn. Het Tuya-toestel zelf wordt
    niet meer aan andere installaties aangeboden (linkhub), anders omzeilen ze het blok.
  * **Log.** Elke sessie krijgt een regel: wie, start, einde, duur, kWh, kosten en waarom ze eindigde. Een sessie die aan de
    laadpaal zelf begint (kaart of app van de auto) staat erin als "Lokaal". Zie `status()` en de CSV in de web-API.
  * **Doorlopen.** Valt de verbinding met de starter weg, dan loopt de sessie door. Ze eindigt pas als de laadpaal zelf zegt dat
    het gedaan is, of als de starter (of de eigenaar) stopt. De sessie staat in /data/charge.json, dus ook een herstart houdt haar.

De instellingen (welk toestel, welke datapunten, wanneer starten of stoppen, de prijs per kWh) staan op het blok, niet meer op
de kaart. Een kaart wijst alleen nog naar een laadpaal (`k:<id>` hier, of `r:<installatie>:k:<id>` bij een andere).

**Een Smappee-laadpaal** (`espdev` is het HUISDEV-blok van het laadpunt, zie smappee.py) werkt anders, want met een badge (RFID) start de eigenaar van de tag aan de laadpaal en de
huisconfig kan niet starten of stoppen:

  * **Geen start en stop**, wel **pauzeren, hervatten en de stroom** (ampère). Een ander huis mag dat alleen tijdens zijn eigen sessie; de eigenaar altijd.
  * **De sessies komen uit de cloud van Smappee**, met de badge erbij. Een tag hoort bij een huis (`tags`: tag -> {by, name}): dat huis krijgt de kWh en de kosten, en de factuur splitst
    daarop. Een badge zonder huis staat in het log als onbekend en kan later een naam krijgen; het log van die badge wordt dan opnieuw toegewezen. Een sessie zonder badge is "Zonder badge".
  * Een sessie telt vanaf `since` (standaard het moment van koppelen): wat Smappee over de week ervoor meldt, boekt niet vanzelf. De sessies van het laadpunt worden niet dubbel geboekt (`imported`).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import discovery as d
from . import smappee
from .cards import CHARGING, same
from .engine import Engine
from .link import split_ref
from .model import TUYA_ID_RE, ModelError, _dp, _num, _opt_entity, _status_list
from .points import Points
from .tuyahub import FEYREE_PID, PRODUCTS

log = logging.getLogger("kk.charge")

MAX_CHARGERS = 8
MAX_LOG = 500
MAX_IMPORTED = 300
START_GRACE = 180.0  # s: na een start wachten tot de laadpaal echt laadt; anders is het niet gelukt
END_AFTER = 20.0  # s: zolang de laadpaal niet meer laadt voor de sessie eindigt (een korte hapering is geen einde)
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
LOCAL = "-"  # starter van een sessie die aan de laadpaal zelf begon
INPUTS = ("status", "time", "energy")
TAG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
HOUSE_RE = re.compile(r"^[a-z0-9_]{1,40}$")
MAX_TAGS = 40
UNKNOWN = "?"  # begin van het huis-id van een badge die nog bij geen huis hoort
NO_TAG = "Zonder badge"
SM_STATUS = {smappee.S_FREE: "free", smappee.S_CONNECTED: "connect", smappee.S_CHARGING: "charging", smappee.S_PAUSED: "paused"}  # de woorden die de kaart kent (cards.STATUS)
SM_OPS = {"pause": ("paused", True), "resume": ("paused", False), "current": ("current", None)}
FEYREE = {"inputs": {"status": "101", "time": "120", "energy": "25"}, "target": {"dp": "124", "on": "OpenCharging", "off": "CloseCharging"}}


def charger_id(device: str) -> str:
    """Het id van de laadpaal op een Tuya-toestel. Een functie van het toestel alleen, zodat een kaart bij een andere installatie
    (die het toestel van vroeger kent) bij de omzetting hetzelfde blok aanwijst."""
    return re.sub(r"[^a-z0-9_]+", "_", device.lower()).strip("_")[:40] or "laadpaal"


def _slug_house(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40]


def _tags(raw) -> dict[str, dict]:
    """{badge: {by, name}}: bij welk huis een badge hoort. `by` is "" (deze installatie), het id van een andere installatie, of een eigen id; zonder `by` komt het uit de naam."""
    out: dict[str, dict] = {}
    for tag, v in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(tag, str) or not TAG_RE.match(tag) or not isinstance(v, dict):
            continue
        name = str(v.get("name") or "").strip()[:60]
        by = v.get("by")
        by = _slug_house(name) if by is None else str(by)
        if name and (by == "" or HOUSE_RE.match(by)) and len(out) < MAX_TAGS:
            out[tag] = {"by": by, "name": name}
    return out


def _defaults(b: dict, device: str = "") -> dict:
    ins, tgt = b.get("inputs") if isinstance(b.get("inputs"), dict) else {}, b.get("target") if isinstance(b.get("target"), dict) else {}
    return {"name": str(b.get("name") or "Laadpaal")[:60], "device": str(b.get("device") or device or ""),
            "inputs": {k: str(ins[k]) for k in INPUTS if ins.get(k)},
            "target": {"dp": str(tgt.get("dp") or ""), "on": str(tgt.get("on") or ""), "off": str(tgt.get("off") or "")},
            "start_when": list(b.get("start_when") or []), "stop_when": list(b.get("stop_when") or []),
            "price": float(b.get("price", 0.30)), "price_entity": b.get("price_entity") or "",
            "espdev": str(b.get("espdev") or ""), "tags": _tags(b.get("tags")), "since": float(b["since"]) if isinstance(b.get("since"), (int, float)) and not isinstance(b.get("since"), bool) else 0.0}


class ChargeHub:
    def __init__(self, data_dir: Path, engine: Engine, tuya=None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(data_dir) / "charge.json"
        self.engine, self._clock = engine, clock
        self.tuya = tuya
        self.points = Points(tuya, None)
        self.state: Callable[[str], Any] = lambda _e: None  # een HA-entiteit (de prijs per kWh): ha.states.get
        self.home: Callable[[], str] = lambda: "Dit huis"  # de naam van deze installatie, voor in de log
        self.peer_name: Callable[[str], str] = lambda pid: pid
        self.changed = None  # de MQTT-brug
        self.chargers: dict[str, dict] = {}
        self.active: dict[str, dict] = {}
        self.log: list[dict] = []
        self.espdev = None  # EspDevHub, voor een Smappee-laadpaal: zijn sessies, standen en opdrachten
        self.imported: dict[str, list[str]] = {}  # laadpaal -> de sessies van Smappee die al in het log staan
        self._cloud_sig: dict[str, object] = {}
        self._tasks: set = set()  # opdrachten die nog onderweg zijn (control_nowait)
        self.seq = 0
        self.migrated = False
        self._load()
        engine.charge_view = self.status

    # ------------------------------------------------------------ opslag

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.error("%s onleesbaar: %s", self.path, e)
            return
        try:
            self.chargers = {k: _defaults(v) for k, v in (raw.get("chargers") or {}).items() if ID_RE.match(k) and isinstance(v, dict)}
            self.active = {k: v for k, v in (raw.get("active") or {}).items() if k in self.chargers and isinstance(v, dict)}
            self.log = [e for e in raw.get("log") or [] if isinstance(e, dict)][-MAX_LOG:]
            self.seq, self.migrated = int(raw.get("seq") or 0), bool(raw.get("migrated"))
            self.imported = {k: [str(x) for x in v][-MAX_IMPORTED:] for k, v in (raw.get("imported") or {}).items() if k in self.chargers and isinstance(v, list)}
        except (TypeError, ValueError, AttributeError) as e:
            log.error("%s onleesbaar: %s", self.path, e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"chargers": self.chargers, "active": self.active, "log": self.log[-MAX_LOG:], "seq": self.seq, "migrated": self.migrated,
                "imported": {k: v[-MAX_IMPORTED:] for k, v in self.imported.items() if k in self.chargers}}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".charge-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _poke(self) -> None:
        if self.changed:
            self.changed()
        self.engine._emit("charge", log=False)

    # ------------------------------------------------------------ instellen

    def devices_used(self) -> set[str]:
        """De Tuya-toestellen die een laadpaal beheert: die gaan niet meer rechtstreeks naar andere installaties."""
        return {c["device"] for c in self.chargers.values() if c["device"]}

    def _check(self, c: dict) -> None:
        if not c["name"].strip():
            raise ModelError("geef de laadpaal een naam")
        if c["espdev"]:
            if c["device"] or c["inputs"] or c["target"]["dp"] or c["start_when"] or c["stop_when"]:
                raise ModelError("een Smappee-laadpaal heeft geen Tuya-toestel, datapunten of start- en stopregels")
            if self.espdev is not None and not self._is_smappee(c["espdev"]):
                raise ModelError("kies het blok van een Smappee-laadpunt")
            if c["since"] < 0:
                raise ModelError("ongeldig begin")
        if c["device"] and not TUYA_ID_RE.fullmatch(c["device"]):
            raise ModelError("ongeldig Tuya-toestel")
        for k, dp in c["inputs"].items():
            if k not in INPUTS:
                raise ModelError(f"laadpaal: onbekende ingang {k}")
            _dp(dp)
        _dp(c["target"]["dp"])
        for k in ("on", "off"):
            if len(c["target"][k]) > 40:
                raise ModelError("laadpaal: een waarde van hoogstens 40 tekens")
        both = {s.lower() for s in c["start_when"]} & {s.lower() for s in c["stop_when"]}
        if both:
            raise ModelError(f"laadpaal: de status {sorted(both)[0]!r} kan niet starten én stoppen")
        _num(c["price"], 0, 100, "prijs per kWh")
        _opt_entity(c["price_entity"] or None, "prijs")

    def _is_smappee(self, did: str) -> bool:
        dev = self.espdev.devices.get(did) if self.espdev is not None else None
        return bool(dev and dev["driver"] == "smappee")

    def _fields(self, old: dict, b: dict) -> dict:
        new = {**old, "inputs": dict(old["inputs"]), "target": dict(old["target"]), "tags": dict(old["tags"])}
        if "espdev" in b:
            new["espdev"] = str(b["espdev"] or "")
        if "since" in b:
            v = b["since"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ModelError("laadpaal: begin als een tijdstip (seconden)")
            new["since"] = float(v)
        if "tags" in b:
            if not isinstance(b["tags"], dict) or any(not isinstance(k, str) or not TAG_RE.match(k) for k in b["tags"]):
                raise ModelError("laadpaal: badges als {badge: {naam, huis}}")
            if len(b["tags"]) > MAX_TAGS:
                raise ModelError(f"laadpaal: maximaal {MAX_TAGS} badges")
            clean = _tags(b["tags"])
            if set(clean) != set(b["tags"]):
                raise ModelError("laadpaal: elke badge heeft een naam (en een huis zonder vreemde tekens)")
            new["tags"] = clean
        if "name" in b:
            new["name"] = str(b["name"] or "").strip()[:60]
        if "device" in b:
            if b["device"] and self.tuya is not None and b["device"] not in self.tuya.devices:
                raise ModelError("onbekend Tuya-toestel")
            new["device"] = str(b["device"] or "")
        if "inputs" in b:
            if not isinstance(b["inputs"], dict):
                raise ModelError("laadpaal: inputs moet een object zijn")
            for k, dp in b["inputs"].items():
                if dp in (None, ""):
                    new["inputs"].pop(k, None)
                else:
                    new["inputs"][k] = str(dp)
        if "target" in b:
            if not isinstance(b["target"], dict):
                raise ModelError("laadpaal: target moet een object zijn")
            for k, v in b["target"].items():
                if k not in ("dp", "on", "off"):
                    raise ModelError(f"laadpaal: onbekend veld target.{k}")
                new["target"][k] = str(v if v is not None else "").strip()
        for k in ("start_when", "stop_when"):
            if k in b:
                new[k] = _status_list(b[k])
        if "price" in b:
            new["price"] = _num(b["price"], 0, 100, "prijs per kWh")
        if "price_entity" in b:
            new["price_entity"] = str(b["price_entity"] or "").strip()
        return new

    def add(self, b: dict) -> str:
        if len(self.chargers) >= MAX_CHARGERS:
            raise ModelError(f"maximaal {MAX_CHARGERS} laadpalen")
        base = cid = charger_id(str(b.get("device") or b.get("espdev") or b.get("name") or "laadpaal"))
        n = 2
        while cid in self.chargers:
            cid, n = f"{base[:36]}_{n}", n + 1
        c = self._fields(_defaults({"name": b.get("name") or "Laadpaal"}), b)
        if c["espdev"] and "since" not in b:
            c["since"] = self._clock()  # wat Smappee over de tijd daarvoor meldt, boekt niet vanzelf
        self._check(c)
        self._create(cid, c)
        return cid

    def _create(self, cid: str, c: dict) -> None:
        self.chargers[cid] = c
        self._save()
        self._poke()

    def update(self, cid: str, b: dict) -> None:
        if cid not in self.chargers:
            raise ModelError("onbekende laadpaal")
        old = self.chargers[cid]
        c = self._fields(old, b)
        self._check(c)
        if cid in self.active and (c["device"] != old["device"] or c["espdev"] != old["espdev"]):
            raise ModelError("er loopt een laadsessie: wacht tot ze klaar is voor je een ander toestel kiest")
        if c["espdev"] != old["espdev"] and old["espdev"]:
            raise ModelError("een Smappee-laadpaal houdt zijn laadpunt: voeg een nieuwe laadpaal toe voor een ander")
        self.chargers[cid] = c
        if c["tags"] != old["tags"]:
            self._reattribute(cid, c)
        self._cloud_sig.pop(cid, None)  # de lopende sessie opnieuw toewijzen
        self._save()
        self._poke()

    def remove(self, cid: str) -> bool:
        if cid not in self.chargers:
            return False
        if cid in self.active:
            raise ModelError("er loopt een laadsessie: stop ze eerst")
        del self.chargers[cid]
        self.imported.pop(cid, None)
        self._cloud_sig.pop(cid, None)
        self._save()
        m = self.engine.model
        m.set_schema({**m.schema, "blocks": {k: v for k, v in m.schema["blocks"].items() if k != f"k:{cid}"}})
        self._poke()
        return True

    # ------------------------------------------------------------ omzetten van de kaarten van voor 0.28

    def adopt(self, link=None) -> None:
        """Eenmalig bij de overgang: elke laadpaalkaart met eigen instellingen wordt een kaart die naar een laadpaal-blok wijst.

        Een lader van deze installatie wordt een blok `k:<toestel>`; een lader bij een andere installatie (`r:<inst>:y:<toestel>`)
        wijst naar het blok dat die installatie op dezelfde manier maakt (`r:<inst>:k:<toestel>`). Een Feyree-lader zonder kaart
        krijgt ook een blok, anders is hij voor andere installaties plots weg."""
        m, touched = self.engine.model, False
        for cid, c in list(m.cards.items()):
            leg = c["type"] == "charger" and c.get("legacy")
            if not leg:
                continue
            dev = leg["target"]["device"] or next((r.partition("@")[2] for r in leg["inputs"].values()), "")
            ref = ""
            hit = split_ref(dev) if dev.startswith("r:") else None
            if hit and hit[1].startswith("y:"):
                kid = charger_id(hit[1][2:])
                ref = f"r:{hit[0]}:k:{kid}"
                if link is not None:
                    link.use_unchecked(hit[0], f"k:{kid}")
            elif dev:
                kid = charger_id(dev)
                if kid not in self.chargers:
                    ins = {k: r[2:].partition("@")[0] for k, r in leg["inputs"].items() if r.partition("@")[2] == dev}
                    lost = [k for k, r in leg["inputs"].items() if r.partition("@")[2] != dev]
                    if lost:
                        log.warning("laadpaal %s: %s kwamen van een ander toestel en zijn losgelaten", c["name"], ", ".join(lost))
                    self.chargers[kid] = _defaults({"name": c["name"], "inputs": ins, "target": leg["target"], "start_when": leg["start_when"],
                                                    "stop_when": leg["stop_when"], "price": leg["price"], "price_entity": leg["price_entity"]}, dev)
                    log.info("laadpaalkaart %s omgezet naar het blok k:%s", c["name"], kid)
                ref = f"k:{kid}"
            m.set_card(cid, charger=ref, legacy=None)
            touched = True
        if not self.migrated:
            self.migrated = touched = True
            for t in (self.tuya.status() if self.tuya else []):
                if t.get("product") == PRODUCTS[FEYREE_PID][0] and t["id"] not in self.devices_used() and charger_id(t["id"]) not in self.chargers:
                    self.chargers[charger_id(t["id"])] = _defaults({"name": t["name"], **FEYREE}, t["id"])
                    log.info("Feyree-lader %s kreeg het laadpaal-blok k:%s", t["name"], charger_id(t["id"]))
        if touched:
            self._save()
            self._poke()

    # ------------------------------------------------------------ lezen van de laadpaal

    def _device(self, c: dict) -> dict | None:
        return self.points.device(c["device"]) if c["device"] else None

    def _point(self, c: dict, dp: str) -> dict | None:
        dev = self._device(c)
        return dev["points"].get(str(dp)) if dev and dev["online"] and dp else None

    def online(self, c: dict) -> bool:
        if c["espdev"]:
            return bool(self.espdev is not None and self.espdev.is_online(c["espdev"]))
        dev = self._device(c)
        return bool(dev and dev["online"])

    def status_value(self, c: dict) -> str | None:
        p = self._point(c, c["inputs"].get("status", ""))
        return str(p["value"]) if p and p.get("value") is not None else None

    def charging(self, c: dict) -> bool:
        """Laadt hij? De status zegt laden; zonder (leesbare) status staat het datapunt van start/stop op de startwaarde."""
        st = self.status_value(c)
        if st is not None:
            return st in CHARGING
        p = self._point(c, c["target"]["dp"])
        return bool(p and c["target"]["on"] and same(p.get("value"), c["target"]["on"]))

    def can_start(self, c: dict) -> bool:
        if c["espdev"]:
            return False  # met een badge start je aan de laadpaal

        """Mag je nu starten? Met een lijst bij de laadpaal (start_when / stop_when) en een status beslist de status: een waarde uit
        de startlijst geeft ja, elke andere niets. Zonder lijsten of status: ja zolang hij niet laadt."""
        start, stop = c["start_when"], c["stop_when"]
        st = self.status_value(c)
        if (start or stop) and c["inputs"].get("status"):
            return st is not None and st.strip().lower() in {s.lower() for s in start}
        return not self.charging(c)

    def energy(self, c: dict) -> float | None:
        p = self._point(c, c["inputs"].get("energy", ""))
        v = p.get("value") if p else None
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def tariff(self, c: dict) -> float:
        if c["price_entity"]:
            st = self.state(c["price_entity"])
            try:
                return float(st[0]) if st else c["price"]
            except (TypeError, ValueError):
                return c["price"]
        return c["price"]

    @staticmethod
    def _session_kwh(s: dict) -> float | None:
        e, base = s.get("last_e"), s.get("base")
        return None if e is None or base is None else max(0.0, e - base)

    # ------------------------------------------------------------ wie ziet wat

    def view(self, cid: str, viewer: str | None) -> dict | None:
        """De laadpaal zoals `viewer` hem mag zien. `None` is deze installatie (de eigenaar), anders het id van een andere.

        Tijdens een sessie van een ander ziet een andere installatie alleen dat hij bezet is. Dit is wat over de lijn gaat."""
        c = self.chargers.get(cid)
        if c is None:
            return None
        if c["espdev"]:
            return self._view_smappee(cid, c, viewer)
        s = self.active.get(cid)
        online = self.online(c)
        base = {"kind": "charger", "name": c["name"], "online": online}
        if s is None:
            phase = "free"
        elif viewer is None:
            phase = "mine" if s["by"] == "" else "other"
        else:
            phase = "mine" if s["by"] == viewer else "busy"
        if phase == "busy":
            return {**base, "phase": "busy", "act": None}
        if not online:
            return {**base, "phase": phase, "act": None}
        out = {**base, "phase": phase, "status": self.status_value(c), "charging": self.charging(c), "act": None, "time": None, "energy": None, "cost": None}
        if s is None:
            out["act"] = "start" if self.can_start(c) and c["target"]["dp"] and c["target"]["on"] else None
        else:
            self._sample(c, s)  # de meterstand van nu, niet die van de laatste tik
            kwh = self._session_kwh(s)
            p = self._point(c, c["inputs"].get("time", ""))
            out.update(act="stop" if c["target"]["dp"] and c["target"]["off"] else None, energy=kwh, time=self.points.text(c["device"], c["inputs"]["time"]) if p else None,
                       cost=None if kwh is None else round(kwh * self.tariff(c), 2), since=int(s["start"]))
            if phase == "other":
                out["by"] = s["name"]
        return out

    def export(self, viewer: str) -> dict[str, dict]:
        """Wat deze installatie aan een andere laat zien: `k:<id>` -> haar eigen kijk op de laadpaal."""
        return {f"k:{cid}": self.view(cid, viewer) for cid in self.chargers}

    # ------------------------------------------------------------ bedienen

    def who_self(self) -> dict:
        return {"id": "", "name": self.home()}

    def who_peer(self, pid: str) -> dict:
        return {"id": pid, "name": self.peer_name(pid)}

    def start(self, cid: str, who: dict) -> None:
        c = self.chargers.get(cid)
        if c is None:
            raise ModelError("onbekende laadpaal")
        if c["espdev"]:
            raise ModelError("starten gaat met de badge aan de laadpaal, niet via de huisconfig")
        if cid in self.active:
            raise ModelError("de laadpaal is bezet")
        if not self.online(c):
            raise ModelError("de laadpaal is niet bereikbaar")
        if not (c["target"]["dp"] and c["target"]["on"]):
            raise ModelError("start/stop is niet ingesteld op de laadpaal")
        if not self.can_start(c):
            raise ModelError("de status van de laadpaal laat geen start toe")
        self.points.set(c["device"], c["target"]["dp"], c["target"]["on"])  # ModelError als het toestel het weigert
        e = self.energy(c)
        self.active[cid] = {"by": who["id"], "name": who["name"], "start": self._clock(), "seen": False, "last_chg": None, "base": e, "last_e": e}
        log.info("laadpaal %s: gestart door %s", c["name"], who["name"])
        self._save()
        self._poke()

    def stop(self, cid: str, who: dict) -> None:
        c = self.chargers.get(cid)
        s = self.active.get(cid)
        if c is None:
            raise ModelError("onbekende laadpaal")
        if c["espdev"]:
            raise ModelError("stoppen gaat met de badge aan de laadpaal; hier kun je pauzeren")
        if s is None:
            raise ModelError("er loopt geen laadsessie")
        if who["id"] != "" and who["id"] != s["by"]:
            raise ModelError("alleen het huis dat gestart heeft kan stoppen")
        if not self.online(c):
            raise ModelError("de laadpaal is niet bereikbaar")
        if not (c["target"]["dp"] and c["target"]["off"]):
            raise ModelError("start/stop is niet ingesteld op de laadpaal")
        self.points.set(c["device"], c["target"]["dp"], c["target"]["off"])
        self._sample(c, s)
        reason = "gestopt" if who["id"] == "" and s["by"] == "" else "gestopt door de eigenaar" if who["id"] == "" else "gestopt door de starter"
        self._end(cid, reason, who["name"])

    # ------------------------------------------------------------ een Smappee-laadpaal: pauzeren en de stroom

    def _sm_point(self, c: dict, pid: str):
        return self.espdev.point_value(c["espdev"], pid) if self.espdev is not None else None

    def _sm_range(self, c: dict) -> tuple[int | None, int | None]:
        dev = self.espdev.devices.get(c["espdev"]) if self.espdev is not None else None
        p = next((x for x in dev["points"] if x["id"] == "current"), None) if dev else None
        return (p["min"], p["max"]) if p else (None, None)

    @staticmethod
    def _hm(seconds: float) -> str:
        m = int(max(0, seconds) // 60)
        return f"{m // 60}:{m % 60:02d}"

    def _view_smappee(self, cid: str, c: dict, viewer: str | None) -> dict:
        """De Smappee-laadpaal zoals `viewer` hem mag zien. Zoals bij een Tuya-laadpaal: tijdens de sessie van een ander alleen "bezet". `act` is `pause` of `resume` (nooit start of stop),
        alleen voor de eigenaar of voor het huis dat nu laadt."""
        s = self.active.get(cid)
        online = self.online(c)
        base = {"kind": "charger", "name": c["name"], "online": online, "ctl": True}
        if s is None:
            phase = "free"
        elif viewer is None:
            phase = "mine" if s["by"] == "" else "other"
        else:
            phase = "mine" if s["by"] == viewer else "busy"
        if phase == "busy":
            return {**base, "phase": "busy", "act": None}
        if not online:
            return {**base, "phase": phase, "act": None}
        st = self._sm_point(c, "state")
        paused, charging, amps = st == smappee.S_PAUSED, st == smappee.S_CHARGING, self._sm_point(c, "current")
        lo, hi = self._sm_range(c)
        may = viewer is None or phase == "mine"
        out = {**base, "phase": phase, "status": SM_STATUS.get(st), "charging": charging, "act": ("resume" if paused else "pause" if charging else None) if may else None,
               "time": None, "energy": None, "cost": None, "amps": amps if may else None, "amp_min": lo, "amp_max": hi}
        if s is not None:
            kwh = s.get("last_e")
            out.update(energy=kwh, time=self._hm(self._clock() - s["start"]), cost=None if kwh is None else round(kwh * self.tariff(c), 2), since=int(s["start"]))
            if phase == "other":
                out["by"] = s["name"]
        return out

    def _sm_check(self, cid: str, who: dict) -> dict:
        c = self.chargers.get(cid)
        if c is None:
            raise ModelError("onbekende laadpaal")
        if not c["espdev"]:
            raise ModelError("dit is geen Smappee-laadpaal: gebruik start en stop")
        if who["id"] != "":
            s = self.active.get(cid)
            if s is None or s["by"] != who["id"]:
                raise ModelError("alleen het huis dat laadt kan dit bedienen")
        if not self.online(c):
            raise ModelError("de laadpaal is niet bereikbaar")
        return c

    @staticmethod
    def _sm_value(op: str, value):
        if op not in SM_OPS:
            raise ModelError("onbekende opdracht")
        pid, v = SM_OPS[op]
        if op == "current":
            try:
                v = float(value)
            except (TypeError, ValueError):
                raise ModelError("de stroom is een getal in ampère") from None
        return pid, v

    async def control(self, cid: str, who: dict, op: str, value=None) -> None:
        """`pause`, `resume` of `current` (ampère) voor een Smappee-laadpaal, en wacht op het antwoord van Smappee (een weigering is een ModelError)."""
        c = self._sm_check(cid, who)
        pid, v = self._sm_value(op, value)
        await self.espdev.set(c["espdev"], pid, v)
        log.info("laadpaal %s: %s%s door %s", c["name"], op, f" {v:g} A" if op == "current" else "", who["name"])

    def control_nowait(self, cid: str, who: dict, op: str, value=None) -> None:
        """Hetzelfde voor wie niet kan wachten (het paneel, een ander huis): de regels worden meteen gecontroleerd, het versturen gebeurt daarna; een mislukking staat in het log."""
        c = self._sm_check(cid, who)
        pid, v = self._sm_value(op, value)
        name = c["name"]

        async def go():
            try:
                await self.espdev.set(c["espdev"], pid, v)
                log.info("laadpaal %s: %s%s door %s", name, op, f" {v:g} A" if op == "current" else "", who["name"])
            except ModelError as e:
                log.warning("laadpaal %s: %s niet gelukt: %s", name, op, e)

        task = asyncio.get_running_loop().create_task(go())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ een Smappee-laadpaal: de sessies uit de cloud en de badge

    def _who_tag(self, c: dict, tag: str) -> dict:
        if not tag:
            return {"by": LOCAL, "name": NO_TAG}
        t = c["tags"].get(tag)
        if t:
            return {"by": t["by"], "name": t["name"]}
        key = re.sub(r"[^a-z0-9]", "", tag.lower())[-12:]
        return {"by": f"{UNKNOWN}{key}", "name": f"Onbekende badge …{tag[-4:]}"}

    def _reattribute(self, cid: str, c: dict) -> None:
        """Een badge kreeg een naam of een ander huis: het log van die badge wordt opnieuw toegewezen (zodat de factuur klopt)."""
        n = 0
        for e in self.log:
            if e["charger"] == cid and e.get("tag"):
                who = self._who_tag(c, e["tag"])
                if (e["by"], e["name"]) != (who["by"], who["name"]):
                    e["by"], e["name"], n = who["by"], who["name"], n + 1
        if n:
            log.info("laadpaal %s: %d sessies opnieuw toegewezen na een wijziging van de badges", c["name"], n)

    def _import(self, cid: str, c: dict, x: dict) -> None:
        who = self._who_tag(c, x["tag"])
        self.seq += 1
        entry = {"n": self.seq, "charger": cid, "by": who["by"], "name": who["name"], "start": int(x["start"]), "end": int(x["end"]), "seconds": int(x["end"] - x["start"]),
                 "kwh": None if x["kwh"] is None else round(x["kwh"], 3), "tariff": self.tariff(c), "reason": "sessie uit Smappee", "stopped_by": "", "tag": x["tag"]}
        entry["cost"] = None if entry["kwh"] is None else round(entry["kwh"] * entry["tariff"], 2)
        self.log.append(entry)
        del self.log[:-MAX_LOG]
        log.info("laadpaal %s: sessie van %s uit Smappee geboekt (%s kWh)", c["name"], who["name"], entry["kwh"])

    def _cloud(self, cid: str, c: dict) -> None:
        """De sessies van Smappee naar het log: afgeronde sessies worden geboekt (een keer), een sessie die loopt is de actieve sessie. Zonder verbinding met Smappee gebeurt er niets."""
        sess = self.espdev.smappee_sessions(c["espdev"]) if self.espdev is not None else None
        if sess is None:
            return
        sig = (tuple((x["key"], x["end"], x["kwh"], x["tag"]) for x in sess[:60]), json.dumps(c["tags"], sort_keys=True))
        if sig == self._cloud_sig.get(cid):
            return
        self._cloud_sig[cid] = sig
        done = self.imported.setdefault(cid, [])
        known, changed, running = set(done), False, None
        for x in reversed(sess):  # de oudste eerst: het log blijft in volgorde
            if x["start"] < c["since"]:
                continue
            if x["end"] is None:
                running = x
                continue
            if x["key"] in known:
                continue
            self._import(cid, c, x)
            done.append(x["key"])
            known.add(x["key"])
            changed = True
        del done[:-MAX_IMPORTED]
        cur = self.active.get(cid)
        if running is not None:
            who = self._who_tag(c, running["tag"])
            s = {"by": who["by"], "name": who["name"], "start": running["start"], "seen": True, "last_chg": self._clock(), "base": 0.0, "last_e": running["kwh"], "cloud": running["key"], "tag": running["tag"]}
            if cur is None or {k: v for k, v in cur.items() if k != "last_chg"} != {k: v for k, v in s.items() if k != "last_chg"}:
                self.active[cid], changed = s, True
        elif cur is not None and cur.get("cloud"):
            del self.active[cid]  # de sessie is voorbij (of verdwenen uit de lijst)
            changed = True
        if changed:
            self._save()
        self._poke()

    # ------------------------------------------------------------ de sessies

    def _sample(self, c: dict, s: dict) -> None:
        """De meterstand van de laadpaal in de sessie. Daalt hij, dan is de teller opnieuw begonnen (een nieuwe sessie op de laadpaal)."""
        e = self.energy(c)
        if e is None:
            return
        if s.get("last_e") is not None and e < s["last_e"] - 1e-9:
            s["base"] = 0.0
        if s.get("base") is None:
            s["base"] = e
        s["last_e"] = e

    def _end(self, cid: str, reason: str, stopped_by: str = "") -> None:
        c, s, now = self.chargers[cid], self.active.pop(cid), self._clock()
        kwh = self._session_kwh(s)
        self.seq += 1
        entry = {"n": self.seq, "charger": cid, "by": s["by"], "name": s["name"], "start": int(s["start"]), "end": int(now), "seconds": int(now - s["start"]),
                 "kwh": None if kwh is None else round(kwh, 3), "tariff": self.tariff(c), "reason": reason, "stopped_by": stopped_by}
        entry["cost"] = None if kwh is None else round(kwh * entry["tariff"], 2)
        self.log.append(entry)
        del self.log[:-MAX_LOG]
        log.info("laadpaal %s: sessie van %s voorbij (%s, %s kWh)", c["name"], s["name"], reason, entry["kwh"])
        self._save()
        self._poke()

    def tick(self) -> None:
        """Elke seconde: een sessie die aan de laadpaal zelf begint opmerken, de meter volgen, en een sessie laten eindigen als de laadpaal klaar is."""
        now = self._clock()
        for cid, c in list(self.chargers.items()):
            if c["espdev"]:
                self._cloud(cid, c)
                continue
            if not self.online(c):
                continue  # niets weten is niet hetzelfde als gestopt: de sessie loopt door
            chg, s = self.charging(c), self.active.get(cid)
            if s is None:
                if chg:
                    e = self.energy(c)
                    self.active[cid] = {"by": LOCAL, "name": "Lokaal (aan de laadpaal)", "start": now, "seen": True, "last_chg": now, "base": e, "last_e": e}
                    log.info("laadpaal %s: laadt zonder dat de add-on startte, genoteerd als lokaal", c["name"])
                    self._save()
                    self._poke()
                continue
            before = (s.get("last_e"), s["seen"])
            self._sample(c, s)
            if chg:
                s["seen"], s["last_chg"] = True, now
            if s["seen"] and not chg and now - s["last_chg"] >= END_AFTER:
                self._end(cid, "klaar of losgekoppeld")
            elif not s["seen"] and now - s["start"] >= START_GRACE:
                self._end(cid, "niet gestart")
            elif before != (s.get("last_e"), s["seen"]):
                self._poke()

    # ------------------------------------------------------------ voor de pagina

    def status(self) -> list[dict]:
        out = []
        for cid, c in self.chargers.items():
            s = self.active.get(cid)
            out.append({"id": cid, "name": c["name"], "device": c["device"], "espdev": c["espdev"], "tags": {k: dict(v) for k, v in c["tags"].items()}, "since": c["since"],
                        "unknown_tags": self._unknown_tags(cid, c), "inputs": dict(c["inputs"]), "target": dict(c["target"]), "start_when": list(c["start_when"]),
                        "stop_when": list(c["stop_when"]), "price": c["price"], "price_entity": c["price_entity"], "online": self.online(c),
                        "view": self.view(cid, None), "session": ({"by": s["by"], "name": s["name"], "start": int(s["start"]), "seen": s["seen"]} if s else None),
                        "log": [e for e in reversed(self.log) if e["charger"] == cid][:100]})
        return out

    def _unknown_tags(self, cid: str, c: dict) -> list[dict]:
        """De badges die in het log of in de lopende sessie voorkwamen en bij geen huis horen: wat de pagina laat toewijzen."""
        seen: dict[str, dict] = {}
        for e in self.log:
            t = e.get("tag")
            if e["charger"] == cid and t and t not in c["tags"]:
                x = seen.setdefault(t, {"tag": t, "sessions": 0, "kwh": 0.0, "last": 0})
                x["sessions"], x["kwh"], x["last"] = x["sessions"] + 1, round(x["kwh"] + (e["kwh"] or 0), 3), max(x["last"], e["end"])
        s = self.active.get(cid)
        if s and s.get("tag") and s["tag"] not in c["tags"]:
            seen.setdefault(s["tag"], {"tag": s["tag"], "sessions": 0, "kwh": 0.0, "last": int(s["start"])})
        return sorted(seen.values(), key=lambda x: -x["last"])

    def sessions(self) -> dict:
        """Alle sessies van alle laadpalen, voor de pagina Laden (filter, CSV en factuur gebeuren daar). `by` is het id van het huis."""
        return {"me": self.home(), "chargers": {cid: c["name"] for cid, c in self.chargers.items()}, "sessions": list(self.log),
                "active": [{"charger": cid, "by": s["by"], "name": s["name"], "start": int(s["start"])} for cid, s in self.active.items()]}

    def csv(self, cid: str | None = None) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["laadpaal", "huis", "start", "einde", "duur (s)", "kWh", "tarief (€/kWh)", "kosten (€)", "reden"])
        stamp = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
        for e in self.log:
            if cid is None or e["charger"] == cid:
                c = self.chargers.get(e["charger"])
                w.writerow([c["name"] if c else e["charger"], e["name"], stamp(e["start"]), stamp(e["end"]), e["seconds"],
                            "" if e["kwh"] is None else f"{e['kwh']:.3f}".replace(".", ","), f"{e['tariff']:.4f}".replace(".", ","),
                            "" if e["cost"] is None else f"{e['cost']:.2f}".replace(".", ","), e["reason"]])
        return buf.getvalue()

    # ------------------------------------------------------------ MQTT: sensoren in HA, voor het energiedashboard

    @staticmethod
    def t(cid: str, what: str) -> str:
        return f"{d.PREFIX}/charge/{cid}/{what}"

    filters: list[str] = []

    def command(self, topic: str, payload: str) -> bool:
        return False

    def discovery(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for cid, c in self.chargers.items():
            oid = f"kk_charge_{cid}"
            device = {"identifiers": [oid], "name": c["name"], "manufacturer": "Huisconfig", "model": "Laadpaal"}
            base = {"device": device, "has_entity_name": True, "availability": [{"topic": d.BRIDGE_STATUS}]}
            out[f"{d.DISCOVERY}/sensor/{oid}_session/config"] = {**base, "name": "Laadsessie", "unique_id": f"{oid}_session", "default_entity_id": f"sensor.{oid}_session",
                                                                 "state_topic": self.t(cid, "session"), "icon": "mdi:ev-station"}
            out[f"{d.DISCOVERY}/sensor/{oid}_last_kwh/config"] = {**base, "name": "Laatste sessie", "unique_id": f"{oid}_last_kwh", "default_entity_id": f"sensor.{oid}_last_kwh",
                                                                  "state_topic": self.t(cid, "last_kwh"), "unit_of_measurement": "kWh", "device_class": "energy"}
            out[f"{d.DISCOVERY}/sensor/{oid}_last_by/config"] = {**base, "name": "Laatste sessie door", "unique_id": f"{oid}_last_by", "default_entity_id": f"sensor.{oid}_last_by",
                                                                 "state_topic": self.t(cid, "last_by"), "icon": "mdi:account"}
        return out

    def states(self) -> dict[str, tuple[str, bool]]:
        out: dict[str, tuple[str, bool]] = {}
        for cid in self.chargers:
            s = self.active.get(cid)
            out[self.t(cid, "session")] = (f"bezet · {s['name']}" if s else "vrij", True)
            last = next((e for e in reversed(self.log) if e["charger"] == cid), None)
            if last:
                if last["kwh"] is not None:
                    out[self.t(cid, "last_kwh")] = (f"{last['kwh']:.3f}", True)
                out[self.t(cid, "last_by")] = (last["name"], True)
        return out
