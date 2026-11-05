"""Omvormers met een Solarman-wifistick (Deye, Sunsynk, ...) rechtstreeks over het LAN, als eigen blok `o:<id>`.

Vervangt de Solarman-integratie van HACS: de add-on leest de registers zelf (solarman.py) en toont op het schema een blok met
uitgangen, te gebruiken in de logica, de routines en de kaarten:

Alle waarden die de omvormer meldt (solarman.FIELDS: zon, accu, net, verbruik, omvormer, generator; per fase, spanning, stroom,
energie van vandaag en totaal) zijn uitgangen. Op het blok staan er standaard zes (zon, verbruik, net, accu, accu %, vandaag); de rest
kies je in het zijpaneel, en elke uitgang is ook als bron te kiezen bij een module. `grid` is positief bij afname, `batt` bij laden.

Alleen lezen: de omvormer zelf stel je hier niet bij. Een schrijfbevel naar een omvormer (werkstand, verkoopvermogen) is niet
terug te draaien als het register niet klopt, en de registers verschillen per model.

De toestellen staan in /data/solar.json (0600). In HA komt per omvormer een apparaat met sensoren (via MQTT-discovery), met
de juiste eenheid en klasse, zodat ze meteen in het energiedashboard passen.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path

import aiohttp

from . import discovery as d
from .engine import Engine
from .logic import NUMBER
from .model import ModelError
from .solarman import FIELDS, PORT, SolarmanClient

log = logging.getLogger("kk.solar")

MAX_DEVICES = 8
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
REF_RE = re.compile(r"^o:([a-z0-9_]{1,40}):([a-z0-9_]{1,12})$")
# elke waarde is een uitgang (te gebruiken in de logica) en een sensor in HA. Op het blok staan er standaard zes; de rest kies je in het zijpaneel.
OUTPUTS = tuple((k, NUMBER, label, unit, group) for k, group, label, unit, *_ in FIELDS)
NAMES = {k for k, *_ in OUTPUTS}
DEFAULT_SHOWN = ("pv", "load", "grid", "batt", "soc", "today")
SENSORS = tuple((k, label[0].upper() + label[1:], unit, cls, state) for k, _g, label, unit, _a, _k, _s, cls, state in FIELDS)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "omvormer"


def _shown(raw) -> list[str]:
    """Welke uitgangen op het blok staan: een lijst namen, in de volgorde van de tabel; ontbreekt ze, dan de standaard."""
    if not isinstance(raw, list):
        return list(DEFAULT_SHOWN)
    return [n for n in NAMES_ORDER if n in raw]


NAMES_ORDER = tuple(k for k, *_ in OUTPUTS)


def _serial(raw) -> int:
    """Het serienummer van de STICK (niet van de omvormer): 8 tot 10 cijfers, staat op de sticker en op de statuspagina."""
    s = str(raw or "").strip()
    if not re.fullmatch(r"\d{6,10}", s) or not 0 < int(s) < 2 ** 32:
        raise ModelError("het serienummer van de stick is 6 tot 10 cijfers, bv. 2983112989 (op de sticker of op zijn statuspagina)")
    return int(s)


class SolarHub:
    def __init__(self, data_dir: Path, engine: Engine, poll: float | None = None, port: int | None = None) -> None:
        self.path = Path(data_dir) / "solar.json"
        self.engine = engine
        self.poll, self.port = poll, port  # alleen voor tests
        self.changed = None  # de MQTT-brug
        self.devices: dict[str, dict] = {}
        self.clients: dict[str, SolarmanClient] = {}
        self.values: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._load()
        engine.solar_view = self.status
        engine.solar_value = self.output

    # ------------------------------------------------------------ opslag

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in (raw.get("devices") or {}).items():
                if isinstance(v, dict) and re.fullmatch(r"[a-z0-9_]{1,40}", k):
                    try:
                        self.devices[k] = {"name": str(v.get("name") or "Omvormer")[:60], "host": str(v.get("host") or ""), "serial": _serial(v.get("serial")),
                                           "shown": _shown(v.get("shown"))}
                    except ModelError:
                        log.error("omvormer %s in %s heeft geen geldig serienummer", k, self.path)
        except FileNotFoundError:
            pass
        except (ValueError, OSError, AttributeError) as e:
            log.error("%s niet leesbaar: %s", self.path, e)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"devices": self.devices}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ toestellen

    def _fields(self, dev: dict, b: dict) -> dict:
        new = dict(dev)
        if "name" in b:
            new["name"] = str(b["name"] or "").strip()[:60]
        if "host" in b:
            new["host"] = str(b["host"] or "").strip()[:253]
        if "serial" in b:
            new["serial"] = _serial(b["serial"])
        if "shown" in b:
            if not isinstance(b["shown"], list) or any(n not in NAMES for n in b["shown"]):
                raise ModelError("onbekende uitgang")
            new["shown"] = _shown(b["shown"])
        if not new["name"]:
            raise ModelError("geef de omvormer een naam")
        if not new["host"] or not HOST_RE.match(new["host"]):
            raise ModelError("geef het IP van de stick, zonder http://")
        if not new.get("serial"):
            raise ModelError("geef het serienummer van de stick")
        return new

    def add(self, b: dict) -> str:
        if len(self.devices) >= MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} omvormers")
        base = did = _slug(str(b.get("name") or "omvormer"))
        n = 2
        while did in self.devices:
            did, n = f"{base}_{n}", n + 1
        dev = self._fields({"name": "Omvormer", "host": "", "serial": 0, "shown": list(DEFAULT_SHOWN)}, b)
        self._unique(did, dev)
        self.devices[did] = dev
        self._save()
        self._start(did)
        self._poke()
        return did

    def _unique(self, did: str, dev: dict) -> None:
        if any(k != did and x["serial"] == dev["serial"] for k, x in self.devices.items()):
            raise ModelError("deze stick staat er al")

    def update(self, did: str, b: dict) -> None:
        if did not in self.devices:
            raise ModelError("onbekende omvormer")
        old = self.devices[did]
        dev = self._fields(old, b)
        self._unique(did, dev)
        self.devices[did] = dev
        self._save()
        if (dev["host"], dev["serial"]) != (old["host"], old["serial"]):
            self._stop(did)
            self.values.pop(did, None)
            self._start(did)
        self._poke()

    def remove(self, did: str) -> bool:
        if did not in self.devices:
            return False
        self._stop(did)
        del self.devices[did]
        self.values.pop(did, None)
        self._save()
        m = self.engine.model
        m.set_schema({**m.schema, "blocks": {k: v for k, v in m.schema["blocks"].items() if k != f"o:{did}"}})
        self._poke()
        return True

    async def lookup(self, host: str, user: str = "admin", password: str = "admin", port: int = 80) -> str:
        """Het serienummer van de stick (`cover_mid`) van zijn statuspagina. De stick vraagt een wachtwoord (standaard admin/admin);
        dat wordt hier alleen voor deze ene vraag gebruikt en niet bewaard."""
        if not HOST_RE.match(host or ""):
            raise ModelError("geef het IP van de stick, zonder http://")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                async with s.get(f"http://{host}:{port}/status.html", headers={"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode("latin1", "replace")).decode()}, allow_redirects=False) as r:
                    if r.status == 401:
                        raise ModelError("de stick weigert de gebruikersnaam of het wachtwoord (standaard admin en admin)")
                    if r.status != 200:
                        raise ModelError(f"de stick antwoordt met {r.status}: is dit een Solarman-stick?")
                    body = (await r.content.read(200_000)).decode("utf-8", "replace")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise ModelError(f"geen verbinding met {host}: {e or 'time-out'}") from None
        m = re.search(r'var cover_mid\s*=\s*"(\d{6,10})"', body)
        if not m:
            raise ModelError("geen serienummer gevonden op de statuspagina: is dit een Solarman-stick?")
        return m.group(1)

    # ------------------------------------------------------------ verbindingen

    def start(self) -> None:
        for did in self.devices:
            self._start(did)

    def _start(self, did: str) -> None:
        dev = self.devices[did]
        c = SolarmanClient(dev["host"], dev["serial"], port=self.port or PORT, poll=self.poll,
                           on_values=lambda v, i=did: self._on_values(i, v), on_link=lambda _up: self._poke())
        self.clients[did] = c
        self.tasks[did] = asyncio.get_running_loop().create_task(c.run())

    def _stop(self, did: str) -> None:
        t = self.tasks.pop(did, None)
        if t:
            t.cancel()
        self.clients.pop(did, None)

    def close(self) -> None:
        for k in list(self.tasks):
            self.tasks.pop(k).cancel()
        self.clients.clear()

    def _on_values(self, did: str, vals: dict) -> None:
        cur = self.values.setdefault(did, {})
        if any(cur.get(k) != v for k, v in vals.items()):
            cur.update(vals)
            self._poke()

    def _poke(self) -> None:
        if self.changed:
            self.changed()
        self.engine._emit("solar", log=False)

    def is_online(self, did: str) -> bool:
        c = self.clients.get(did)
        return bool(c and c.connected)

    # ------------------------------------------------------------ de stand

    def output(self, ref: str):
        """De waarde op een uitgang `o:<id>:<naam>`, of None (onbekend: offline of niet gemeld)."""
        m = REF_RE.match(ref or "")
        if not m or m.group(1) not in self.devices or m.group(2) not in NAMES or not self.is_online(m.group(1)):
            return None
        return self.values.get(m.group(1), {}).get(m.group(2))

    def status(self) -> list[dict]:
        out = []
        for did, dev in self.devices.items():
            c = self.clients.get(did)
            out.append({"id": did, "name": dev["name"], "host": dev["host"], "serial": str(dev["serial"]), "online": self.is_online(did),
                        "error": c.last_error if c else "", "values": dict(self.values.get(did, {})), "shown": list(dev["shown"]),
                        "outputs": {n: self.output(f"o:{did}:{n}") for n, *_ in OUTPUTS}})
        return out

    # ------------------------------------------------------------ MQTT

    @staticmethod
    def t(did: str, what: str) -> str:
        return f"{d.PREFIX}/solar/{did}/{what}/state"

    @property
    def filters(self) -> list[str]:
        return []

    def command(self, topic: str, payload: str) -> bool:
        return False

    def discovery(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for did, dev in self.devices.items():
            device = {"identifiers": [f"kk_solar_{did}"], "name": dev["name"], "manufacturer": "Solarman", "model": "Omvormer (Huisconfig)"}
            for key, name, unit, cls, state in SENSORS:
                oid = f"kk_solar_{did}_{key}"
                out[f"{d.DISCOVERY}/sensor/{oid}/config"] = {
                    "device": device, "has_entity_name": True, "name": name, "unique_id": oid, "default_entity_id": f"sensor.{oid}",
                    "state_topic": self.t(did, key), "unit_of_measurement": unit, "device_class": cls, "state_class": state,
                    "availability_mode": "all", "availability": [{"topic": d.BRIDGE_STATUS}, {"topic": self.t(did, "online")}],
                    "payload_available": "online", "payload_not_available": "offline"}
        return out

    def states(self) -> dict[str, tuple[str, bool]]:
        out: dict[str, tuple[str, bool]] = {}
        for did in self.devices:
            out[self.t(did, "online")] = ("online" if self.is_online(did) else "offline", True)
            for key, *_ in SENSORS:
                v = self.values.get(did, {}).get(key)
                if v is not None:
                    out[self.t(did, key)] = (f"{v:g}", True)
        return out
