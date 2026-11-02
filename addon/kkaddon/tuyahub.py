"""Tuya-toestellen rechtstreeks over het LAN, als entiteiten in HA via MQTT-discovery. Zonder HACS, LocalTuya of cloud.

De toestellen staan in /data/tuya.json en niet in het model: de local keys horen niet in network.json, want die gaat mee in
de export. Het bestand valt wel onder de HA-back-up. De status voor de pagina bevat nooit een sleutel.

Een entiteit bestaat pas als het toestel het datapunt meldt: wat in het datamodel staat maar nooit komt, heeft geen nut.
Schrijven kan alleen op een datapunt dat in het datamodel beschrijfbaar is. Zonder datamodel blijft alles alleen-lezen.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from . import discovery as d
from .engine import Engine
from .model import ModelError
from .tuya import CRYPTO, TuyaClient, listen
from .tuyacloud import REGIONS, TuyaCloud

log = logging.getLogger("kk.tuya")

MAX_DEVICES = 64
SET_RE = re.compile(rf"^{d.PREFIX}/tuya/([^/]+)/(\d+)/set$")
ID_RE = re.compile(r"^[A-Za-z0-9]{10,40}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
VERSIONS = ("auto", "3.3", "3.4", "3.5")
UNITS = {"kW·h": "kWh", "℃": "°C"}
DEVICE_CLASS = {"V": "voltage", "A": "current", "kW": "power", "W": "power", "kWh": "energy", "°C": "temperature"}


def _dp(code, access, kind, name=None, unit="", scale=0, lo=None, hi=None, options=None, hide=False, diag=False) -> dict:
    return {"code": code, "access": access, "type": kind, "name": name or code, "unit": unit, "scale": scale, "min": lo, "max": hi, "options": options or [], "hide": hide, "diag": diag}


# Uit "Query Things Data Model" van product zk1jkjhxqkfaaalp. De stroom staat op 115: deze lader meldt Max32A; de andere Set-punten verbergen we.
FEYREE = {
    "3": _dp("work_state", "ro", "enum", "Werkstand"),
    "10": _dp("fault", "ro", "bitmap", "Foutcode", diag=True),
    "11": _dp("alarm_set_1", "rw", "raw", hide=True),
    "12": _dp("alarm_set_2", "rw", "raw", hide=True),
    "14": _dp("work_mode", "rw", "enum", "Laadmodus", options=["charge_now", "charge_pct", "charge_energy", "charge_schedule"]),
    "15": _dp("balance_energy", "ro", "value", "Beschikbare stroom", "A", 1),
    "16": _dp("clear_energy", "rw", "bool", hide=True),
    "18": _dp("switch", "rw", "bool", "Aan"),
    "23": _dp("system_version", "ro", "string", "Systeemversie", diag=True),
    "25": _dp("charge_energy_once", "ro", "value", "Energie laatste beurt", "kWh", 2),
    "27": _dp("online_state", "rw", "enum", hide=True),
    "101": _dp("DeviceState", "ro", "enum", "Status"),
    "102": _dp("A_Voltage", "ro", "value", "Spanning L1", "V", 1),
    "103": _dp("B_Voltage", "ro", "value", "Spanning L2", "V", 1),
    "104": _dp("C_Voltage", "ro", "value", "Spanning L3", "V", 1),
    "105": _dp("A_Current", "ro", "value", "Stroom L1", "A", 1),
    "106": _dp("B_Current", "ro", "value", "Stroom L2", "A", 1),
    "107": _dp("C_Current", "ro", "value", "Stroom L3", "A", 1),
    "108": _dp("PhaseFlag", "ro", "enum", "Fasen", diag=True),
    "109": _dp("DeviceKw", "ro", "value", "Vermogen", "kW", 1),
    "110": _dp("DeviceTemp", "ro", "value", "Temperatuur", "°C", 1, diag=True),
    "111": _dp("month", "rw", "value", hide=True),
    "112": _dp("DeviceKwh", "ro", "value", "Energie totaal", "kWh", 1),
    "113": _dp("DeviceMaxSetA", "ro", "enum", "Maximale stroom", diag=True),
    "114": _dp("Set16A", "rw", "value", hide=True),
    "115": _dp("Set32A", "rw", "value", "Laadstroom", "A", 0, 8, 32),
    "116": _dp("Set40A", "rw", "value", hide=True),
    "117": _dp("Set50A", "rw", "value", hide=True),
    "118": _dp("SetDelayTime", "rw", "value", "Uitgestelde start", "h", 0, 0, 15),
    "119": _dp("SetDefineTime", "rw", "value", "Ingestelde duur", "h", 0, 0, 15),
    "120": _dp("Ctime", "ro", "string", "Laadtijd"),
    "121": _dp("logupdown", "rw", "enum", hide=True),
    "122": _dp("service", "ro", "string", hide=True),
    "123": _dp("RFID", "rw", "bool", "RFID verplicht"),
    "124": _dp("ChargingOperation", "rw", "enum", "Laden", options=["OpenCharging", "CloseCharging", "WaitOperation"]),
    "125": _dp("hw_version", "ro", "string", "Hardwareversie", diag=True),
    "126": _dp("sw_version", "ro", "string", "Softwareversie", diag=True),
    **{str(n): _dp(f"log_content{n - 126}", "ro", "string", hide=True) for n in range(127, 133)},
}
# Ingebouwde datamodellen met nette namen, per product-id. De cloud meldt het product-id bij het ophalen; dan wint dit van het
# ruwe datamodel, want het weet ook wat verborgen moet en welke grenzen echt gelden.
FEYREE_PID = "zk1jkjhxqkfaaalp"
PRODUCTS = {FEYREE_PID: ("Feyree EV-lader", FEYREE)}
OLD_PROFILES = {"feyree": FEYREE_PID}  # tot 0.10.5 koos je dit zelf
CLOUD_ID_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")
FOUND_MAX = 256
FOUND_KEEP = 30 * 86400  # s: wat zo lang niet meer gemeld is, vergeten we
FOUND_SAVE = 3600  # s: "laatst gezien" hoeft niet bij elke broadcast naar schijf
LIVE = 120  # s: zo recent gemeld telt als nu op het netwerk


def parse_model(text: str) -> dict:
    """Het antwoord van "Query Things Data Model" (met of zonder de omhulling) -> datapunten."""
    try:
        j = json.loads(text)
        if isinstance(j.get("result"), dict):
            j = j["result"]
        if isinstance(j.get("model"), str):
            j = json.loads(j["model"])
        props = [p for s in j["services"] for p in s["properties"]]
        out = {}
        for p in props:
            spec = p["typeSpec"]
            kind = spec["type"]
            out[str(p["abilityId"])] = _dp(p["code"], "ro" if p.get("accessMode") == "ro" else "rw", kind, p["code"], UNITS.get(spec.get("unit"), spec.get("unit") or ""),
                                           int(spec.get("scale") or 0), spec.get("min"), spec.get("max"), spec.get("range") or [], hide=kind in ("raw", "bitmap"))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ModelError(f"dit is geen Tuya-datamodel: {e}")
    if not out:
        raise ModelError("het datamodel bevat geen datapunten")
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "toestel"


class TuyaHub:
    def __init__(self, data_dir: Path, engine: Engine) -> None:
        self.path = Path(data_dir) / "tuya.json"
        self.engine = engine
        self.changed = None  # de MQTT-brug: er is iets nieuws om te publiceren
        self.devices: dict[str, dict] = {}
        self.cloud: dict = {"id": "", "secret": "", "region": "eu"}
        self.cloud_base: str | None = None  # alleen voor tests
        self.found: dict[str, dict] = {}  # device-id -> {"ip", "version", "seen"}, uit de broadcasts, bewaard over herstarts
        self._found_saved: dict[str, float] = {}
        self._udp: list = []
        self.clients: dict[str, TuyaClient] = {}
        self.values: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._load()
        engine.tuya_view = self.status

    # ------------------------------------------------------------ opslag

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.devices = {k: v for k, v in raw.get("devices", {}).items() if isinstance(v, dict)}
            if isinstance(raw.get("cloud"), dict):
                self.cloud.update({k: str(raw["cloud"].get(k) or "") for k in ("id", "secret", "region")})
            for dev in self.devices.values():
                old = dev.pop("profile", "")
                if old in OLD_PROFILES and not dev.get("product"):
                    dev["product"] = OLD_PROFILES[old]
            now = time.time()
            for k, v in (raw.get("found") or {}).items():
                if isinstance(v, dict) and ID_RE.match(k) and HOST_RE.match(str(v.get("ip") or "")) and isinstance(v.get("seen"), (int, float)) and now - v["seen"] < FOUND_KEEP:
                    self.found[k] = {"ip": v["ip"], "version": v.get("version") if v.get("version") in VERSIONS else "", "seen": v["seen"]}
                    self._found_saved[k] = v["seen"]
        except FileNotFoundError:
            pass
        except (ValueError, OSError, AttributeError) as e:
            log.error("%s niet leesbaar: %s", self.path, e)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"devices": self.devices, "cloud": self.cloud, "found": self.found}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ toestellen

    def _check(self, dev: dict) -> None:
        if not dev.get("name"):
            raise ModelError("geef het toestel een naam")
        # een nieuw blok is nog leeg: wat ingevuld is moet kloppen, verbinden doet het pas als alles er is
        if dev.get("device_id") and not ID_RE.match(dev["device_id"]):
            raise ModelError("ongeldig device-id")
        if dev.get("host") and not HOST_RE.match(dev["host"]):
            raise ModelError("ongeldig adres: geef het IP zonder https://")
        try:
            ok = not dev.get("key") or len(dev["key"].encode("latin1")) == 16
        except UnicodeEncodeError:
            ok = False
        if not ok:
            raise ModelError("een local key is precies 16 tekens")
        if dev.get("version") not in VERSIONS:
            raise ModelError("versie moet auto, 3.3, 3.4 of 3.5 zijn")

    def _fields(self, dev: dict, b: dict) -> dict:
        new = dict(dev)
        for k in ("name", "device_id", "host", "version"):
            if k in b:
                new[k] = str(b[k] or "").strip()
        if new.get("device_id") != dev.get("device_id"):
            new["product"] = ""  # een ander toestel: wat de cloud over het vorige zei, geldt niet meer
        known = self.found.get(new.get("device_id") or "")
        if known and not new.get("host"):  # al eens gemeld: niet wachten op de volgende broadcast
            new["host"] = known["ip"]
            if new.get("version") == "auto" and known["version"]:
                new["version"] = known["version"]
        if b.get("key"):  # leeg bij bewerken: de sleutel blijft
            new["key"] = str(b["key"])
        if b.get("model"):
            new["defs"] = parse_model(str(b["model"]))
            new["product"] = ""  # wat je net plakte, wint
        return new

    def _unique(self, did: str, dev: dict) -> None:
        if dev.get("device_id") and any(k != did and x.get("device_id") == dev["device_id"] for k, x in self.devices.items()):
            raise ModelError("dit toestel staat er al")

    def add(self, b: dict) -> str:
        if len(self.devices) >= MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} toestellen")
        dev = self._fields({"name": "Tuya", "device_id": "", "host": "", "key": "", "version": "auto", "product": "", "defs": {}}, b)
        self._check(dev)
        self._unique("", dev)
        base = did = _slug(dev["name"])
        n = 2
        while did in self.devices:
            did, n = f"{base}_{n}", n + 1
        self.devices[did] = dev
        self._save()
        self._start(did)
        self._poke()
        return did

    def update(self, did: str, b: dict) -> None:
        if did not in self.devices:
            raise ModelError("onbekend toestel")
        dev = self._fields(self.devices[did], b)
        self._check(dev)
        self._unique(did, dev)
        self._apply(did, dev)

    def _apply(self, did: str, dev: dict) -> None:
        reconnect = any(dev.get(k) != self.devices[did].get(k) for k in ("device_id", "host", "version", "key"))
        self.devices[did] = dev
        self._save()
        if reconnect:
            self._stop(did)
            self.values.pop(did, None)
            self._start(did)
        self._poke()

    # ------------------------------------------------------------ eenmalig uit de cloud

    def set_cloud(self, b: dict) -> None:
        new = dict(self.cloud)
        for k in ("id", "region"):
            if k in b:
                new[k] = str(b[k] or "").strip()
        if b.get("secret"):  # leeg bij bewerken: het geheim blijft
            new["secret"] = str(b["secret"]).strip()
        if new["id"] and not CLOUD_ID_RE.match(new["id"]):
            raise ModelError("ongeldige Access ID")
        if new["secret"] and not CLOUD_ID_RE.match(new["secret"]):
            raise ModelError("ongeldig Access Secret")
        if new["region"] not in REGIONS:
            raise ModelError("onbekend datacenter")
        self.cloud = new
        self._save()
        self._poke()

    def cloud_ready(self) -> bool:
        return bool(self.cloud["id"] and self.cloud["secret"])

    async def fetch(self, did: str) -> None:
        """Local key, product en datamodel uit de cloud. Het IP niet: de cloud kent alleen het publieke adres."""
        if did not in self.devices:
            raise ModelError("onbekend toestel")
        dev = dict(self.devices[did])
        if not dev.get("device_id"):
            raise ModelError("vul eerst het device-id in")
        if not self.cloud_ready():
            raise ModelError("vul eerst de Access ID en het Access Secret van je Tuya-cloudproject in")
        info, model = await TuyaCloud(self.cloud["id"], self.cloud["secret"], self.cloud["region"], self.cloud_base).device(dev["device_id"])
        if did not in self.devices:
            return  # intussen verwijderd
        dev = dict(self.devices[did])
        if info.get("local_key"):
            dev["key"] = str(info["local_key"])
        dev["product"] = str(info.get("product_id") or "")
        if model:
            dev["defs"] = parse_model(model)
        if dev.get("name") in ("", "Tuya") and info.get("name"):
            dev["name"] = str(info["name"])[:60]
        self._check(dev)
        self._apply(did, dev)

    def remove(self, did: str) -> bool:
        if did not in self.devices:
            return False
        self._stop(did)
        del self.devices[did]
        self.values.pop(did, None)
        self._save()
        m = self.engine.model
        m.set_schema({**m.schema, "blocks": {k: v for k, v in m.schema["blocks"].items() if k != f"y:{did}"}})
        self._poke()
        return True

    # ------------------------------------------------------------ verbindingen

    def start(self) -> None:
        for did in self.devices:
            self._start(did)
        if CRYPTO:
            self.tasks["_listen"] = asyncio.get_running_loop().create_task(self._listen())

    async def _listen(self) -> None:
        self._udp = await listen(self.on_broadcast)

    # ------------------------------------------------------------ zoeken op het LAN

    def on_broadcast(self, found: dict, src: str) -> None:
        """Een toestel meldt zich. Een leeg IP vullen we in; een ander IP alleen als we het toestel niet bereiken (DHCP)."""
        tid = found["id"]
        if not ID_RE.match(tid):
            return
        ip = found["ip"] if HOST_RE.match(found["ip"]) else src
        version = found["version"] if found["version"] in VERSIONS else ""
        now = time.time()
        old = self.found.get(tid)
        new = not old or old["ip"] != ip or old["version"] != version
        was_live = bool(old) and now - old["seen"] < LIVE
        self.found[tid] = {"ip": ip, "version": version, "seen": now}
        if len(self.found) > FOUND_MAX:
            del self.found[min(self.found, key=lambda k: self.found[k]["seen"])]
        if new or now - self._found_saved.get(tid, 0) > FOUND_SAVE:
            self._found_saved[tid] = now
            self._save()
        for did, dev in self.devices.items():
            if dev.get("device_id") != tid or dev.get("host") == ip or (dev.get("host") and self._online(did)):
                continue
            log.info("%s gevonden op %s", dev["name"], ip)
            upd = {**dev, "host": ip}
            if dev.get("version") == "auto" and self.found[tid]["version"]:
                upd["version"] = self.found[tid]["version"]
            self._apply(did, upd)
            return
        if new or not was_live:
            self._poke()

    def _shown_found(self, tid: str) -> dict | None:
        f = self.found.get(tid)
        return f and {"id": tid, "ip": f["ip"], "version": f["version"], "seen": int(f["seen"]), "live": time.time() - f["seen"] < LIVE}

    def _unclaimed(self) -> list[dict]:
        used = {dev.get("device_id") for dev in self.devices.values()}
        return [self._shown_found(k) for k, v in sorted(self.found.items(), key=lambda x: x[1]["ip"]) if k not in used]

    def _start(self, did: str) -> None:
        dev = self.devices[did]
        if not self._complete(dev):
            return
        c = TuyaClient(dev["device_id"], dev["key"], dev["host"], dev.get("version", "auto"),
                       on_dps=lambda dps, i=did: self._on_dps(i, dps), on_link=lambda _up: self._poke(), on_version=lambda v, i=did: self._on_version(i, v))
        self.clients[did] = c
        self.tasks[did] = asyncio.get_running_loop().create_task(c.run())

    @staticmethod
    def _complete(dev: dict) -> bool:
        return bool(dev.get("device_id") and dev.get("host") and dev.get("key"))

    def _stop(self, did: str) -> None:
        t = self.tasks.pop(did, None)
        if t:
            t.cancel()
        self.clients.pop(did, None)

    def close(self) -> None:
        for did in list(self.tasks):
            self._stop(did)
        for t in self._udp:
            t.close()
        self._udp = []

    def _on_version(self, did: str, version: str) -> None:
        if did in self.devices:
            self.devices[did]["version"] = version  # voortaan niet meer zoeken
            self._save()

    def _on_dps(self, did: str, dps: dict) -> None:
        vals = self.values.setdefault(did, {})
        fresh = {str(k): v for k, v in dps.items()}
        if any(k not in vals or vals[k] != v for k, v in fresh.items()):
            vals.update(fresh)
            self._poke()

    def _poke(self) -> None:
        if self.changed:
            self.changed()
        self.engine._emit("tuya", log=False)

    def _defs(self, did: str) -> dict:
        dev = self.devices[did]
        return PRODUCTS[dev["product"]][1] if dev.get("product") in PRODUCTS else dev.get("defs") or {}

    def _online(self, did: str) -> bool:
        c = self.clients.get(did)
        return bool(c and c.connected)

    # ------------------------------------------------------------ voor de pagina: nooit de sleutel

    def status(self) -> list[dict]:
        out = []
        for did, dev in self.devices.items():
            c = self.clients.get(did)
            defs = self._defs(did)
            points = []
            for dp, v in sorted(self.values.get(did, {}).items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
                de = defs.get(dp) or {}
                scale = de.get("scale") or 0
                # instelbaar is alleen wat `convert` ook echt toelaat: raw of een getal zonder grenzen niet
                rw = de.get("access") == "rw" and not de.get("hide") and (de.get("type") == "bool" or (de.get("type") == "enum" and bool(de.get("options"))) or (de.get("type") == "value" and de.get("min") is not None and de.get("max") is not None))
                points.append({"dp": dp, "code": de.get("code") or "", "name": de.get("name") or f"Datapunt {dp}", "value": self._shown(de, v), "unit": de.get("unit", ""), "rw": rw, "hide": bool(de.get("hide")) or de.get("type") == "raw", "diag": bool(de.get("diag")),
                               "type": de.get("type") or "", "options": de.get("options") or [],
                               "min": de["min"] / 10 ** scale if rw and de.get("min") is not None else None, "max": de["max"] / 10 ** scale if rw and de.get("max") is not None else None, "step": 1 / 10 ** scale})
            out.append({"id": did, "name": dev["name"], "device_id": dev.get("device_id", ""), "host": dev.get("host", ""), "version": c.version if c else dev.get("version"),
                        "product": PRODUCTS[dev["product"]][0] if dev.get("product") in PRODUCTS else "", "has_key": bool(dev.get("key")), "configured": self._complete(dev),
                        "has_model": bool(dev.get("defs")) or dev.get("product") in PRODUCTS, "online": self._online(did), "error": c.last_error if c else "",
                        "points": points, "available": CRYPTO, "cloud": {"id": self.cloud["id"], "region": self.cloud["region"], "has_secret": bool(self.cloud["secret"])},
                        "found": self._shown_found(dev.get("device_id") or ""), "unclaimed": self._unclaimed()})
        return out

    @staticmethod
    def _shown(de: dict, v):
        if de.get("type") == "value" and isinstance(v, (int, float)) and not isinstance(v, bool):
            return v / 10 ** de["scale"] if de.get("scale") else v
        return v

    # ------------------------------------------------------------ MQTT

    @staticmethod
    def t_online(did: str) -> str:
        return f"{d.PREFIX}/tuya/{did}/online"

    @staticmethod
    def t_state(did: str, dp: str) -> str:
        return f"{d.PREFIX}/tuya/{did}/{dp}/state"

    @property
    def filters(self) -> list[str]:
        return [f"{d.PREFIX}/tuya/+/+/set"]

    def _config(self, did: str, dp: str, v, de: dict) -> tuple[str, dict] | None:
        """(domein, specifieke velden) voor een datapunt, of None als het geen entiteit wordt."""
        if de.get("hide") or (de.get("type") == "raw"):
            return None
        rw = de.get("access") == "rw"
        kind = de.get("type") or ("bool" if isinstance(v, bool) else "value" if isinstance(v, (int, float)) else "string")
        out: dict = {}
        scale = de.get("scale") or 0
        if kind == "bool":
            domain = "switch" if rw else "binary_sensor"
            out.update(payload_on="ON", payload_off="OFF")
        elif rw and kind == "enum" and de.get("options"):
            domain = "select"
            out["options"] = de["options"]
        elif rw and kind == "value" and de.get("min") is not None and de.get("max") is not None:
            domain = "number"
            out.update(min=de["min"] / 10 ** scale, max=de["max"] / 10 ** scale, step=1 / 10 ** scale, mode="box")
        elif kind == "value":
            domain = "sensor"
        else:
            domain = "sensor"
        if rw and domain in ("switch", "select", "number"):
            out["command_topic"] = f"{d.PREFIX}/tuya/{did}/{dp}/set"
        unit = de.get("unit")
        if unit:
            out["unit_of_measurement"] = unit
        if domain == "sensor" and kind == "value":
            cls = DEVICE_CLASS.get(unit)
            if cls:
                out["device_class"] = cls
            if cls == "energy":
                out["state_class"] = "total_increasing"
            elif unit:
                out["state_class"] = "measurement"
        elif domain == "number" and DEVICE_CLASS.get(unit):
            out["device_class"] = DEVICE_CLASS[unit]
        if de.get("diag"):
            out["entity_category"] = "diagnostic"
        return domain, out

    def discovery(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for did, dev in self.devices.items():
            device = {"identifiers": [f"kk_tuya_{did}"], "name": dev["name"], "manufacturer": "Tuya", "model": PRODUCTS[dev["product"]][0] if dev.get("product") in PRODUCTS else "Tuya (Huisconfig)"}
            defs = self._defs(did)
            for dp, v in self.values.get(did, {}).items():
                de = defs.get(dp) or {}
                made = self._config(did, dp, v, de)
                if not made:
                    continue
                domain, extra = made
                oid = f"kk_tuya_{did}_{dp}"
                out[f"{d.DISCOVERY}/{domain}/{oid}/config"] = {
                    "device": device, "has_entity_name": True, "name": de.get("name") or f"Datapunt {dp}", "unique_id": oid, "default_entity_id": f"{domain}.{oid}",
                    "state_topic": self.t_state(did, dp), "availability_mode": "all", "availability": [{"topic": d.BRIDGE_STATUS}, {"topic": self.t_online(did)}], **extra}
        return out

    def states(self) -> dict[str, tuple[str, bool]]:
        out: dict[str, tuple[str, bool]] = {}
        for did in self.devices:
            out[self.t_online(did)] = ("online" if self._online(did) else "offline", True)
            defs = self._defs(did)
            for dp, v in self.values.get(did, {}).items():
                de = defs.get(dp) or {}
                shown = self._shown(de, v)
                text = ("ON" if shown else "OFF") if isinstance(shown, bool) else str(shown)
                out[self.t_state(did, dp)] = (text, True)
        return out

    def command(self, topic: str, payload: str) -> bool:
        m = SET_RE.match(topic)
        if not m:
            return False
        try:
            self.set(*m.groups(), payload)
        except ModelError as e:
            log.warning("%s datapunt %s: %s", m.group(1), m.group(2), e)
        return True

    def set(self, did: str, dp: str, payload: str) -> None:
        """Eén opdracht, uit HA of uit de zijbalk. De stand verandert pas als het toestel hem meldt."""
        if did not in self.devices:
            raise ModelError("onbekend toestel")
        value = self.convert(self._defs(did).get(dp), payload)
        if value is None:
            raise ModelError(f"opdracht {payload!r} is niet toegestaan")
        c = self.clients.get(did)
        if not c or not c.set_dps({dp: value}):
            raise ModelError("niet verstuurd: geen verbinding met het toestel")

    @staticmethod
    def convert(de: dict | None, payload: str):
        """HA-waarde -> waarde voor het toestel, of None als het niet mag. Alleen wat het datamodel beschrijfbaar noemt."""
        if not de or de.get("access") != "rw" or de.get("hide"):
            return None
        kind = de.get("type")
        if kind == "bool":
            return {"ON": True, "OFF": False}.get(payload.upper())
        if kind == "enum":
            return payload if payload in de.get("options", []) else None
        if kind == "value":
            try:
                raw = round(float(payload) * 10 ** (de.get("scale") or 0))
            except (ValueError, OverflowError):
                return None
            if de.get("min") is None or de.get("max") is None or not de["min"] <= raw <= de["max"]:
                return None
            return raw
        return None
