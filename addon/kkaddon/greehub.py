"""Airco's met een Gree-wifimodule (Toyotomi, Gree, Sinclair, ...) rechtstreeks over het LAN, als eigen blok `w:<id>`.

Vervangt de HACS-integratie: in HA verschijnt per airco een `climate`-entiteit (via MQTT-discovery) met schakelaars voor
turbo, stil, licht en dergelijke. Op het schema heeft het blok eigen poorten:

  links   aan (0/1), temperatuur (getal), blokkeren (0/1)   een timer, module of waarde stuurt de airco
  rechts  aan (0/1), kamer (getal), ingesteld (getal)       `w:<id>:on|room|set`, voor de logica en de routines

Een ingang handelt op een **wijziging**, nooit op een stand, zoals de routine Tuya instellen (actions.py): de eerste stand na
een herstart of na onbekend wordt alleen onthouden. Zo zet een herstart de airco niet aan of uit, en wint wat je met de
afstandsbediening deed tot de ingang weer verandert. Blokkeren op 1 zet de airco uit en houdt de ingang aan tegen; terug op 0
gaat hij weer aan als de ingang aan dan 1 is.

De toestellen en hun sleutels staan in /data/gree.json (0600), niet in het model: zoals bij Tuya gaat network.json mee in de
export. De status voor de pagina bevat nooit een sleutel.
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
from .gree import CRYPTO, MAC_RE, VERSIONS, GreeClient, norm_mac, scan, scan_socket
from .logic import BOOL, NUMBER
from .model import ModelError, source_type

log = logging.getLogger("kk.gree")

MAX_DEVICES = 16
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
MAC_IN_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2}){5}$")
REF_RE = re.compile(r"^w:([a-z0-9_]{1,40}):(on|room|set)$")
SET_RE = re.compile(rf"^{d.PREFIX}/gree/([a-z0-9_]+)/([a-z0-9_]+)/set$")
SCAN_EVERY = 60  # s: zolang een airco geen sleutel heeft of onbereikbaar is
FOUND_KEEP = 3600  # s: wat zich zo lang niet meer meldde, verdwijnt uit de keuzelijst
RETRY = 30.0  # s: een ingang die niet doorkwam, opnieuw proberen
TICK = 1.0  # s
MIN_T, MAX_T = 16, 30

MODES = ["auto", "cool", "dry", "fan_only", "heat"]  # Mod 0..4, met de namen van HA
FANS = ["auto", "low", "medium_low", "medium", "medium_high", "high"]  # WdSpd 0..5
SWING_V = ["default", "swing", "top", "upper_middle", "middle", "lower_middle", "bottom",
           "swing_top", "swing_upper_middle", "swing_middle", "swing_lower_middle", "swing_bottom"]  # SwUpDn 0..11
SWING_H = ["default", "swing", "left", "left_center", "center", "right_center", "right"]  # SwingLfRig 0..6
# veld -> (eigenschap van Gree, keuzes of None voor aan/uit, naam)
SWITCHES = {"turbo": ("Tur", "Turbo"), "quiet": ("Quiet", "Stil"), "light": ("Lig", "Licht"), "xfan": ("Blo", "X-fan (nadrogen)"),
            "health": ("Health", "Health"), "save": ("SvSt", "Energiebesparing"), "heat8": ("StHt", "8 °C-verwarming"), "sleep": ("SwhSlp", "Slaapstand")}
CHOICES = {"mode": ("Mod", MODES), "fan": ("WdSpd", FANS), "swing_v": ("SwUpDn", SWING_V), "swing_h": ("SwingLfRig", SWING_H)}
# de ingangen: (naam, soort, label)
INPUTS = (("on", BOOL, "aan"), ("temp", NUMBER, "temperatuur"), ("block", BOOL, "blokkeren"))
OUTPUTS = (("on", BOOL, "aan"), ("room", NUMBER, "kamer"), ("set", NUMBER, "ingesteld"))
ON_MODES = ("keep", *MODES)  # wat de ingang aan kiest: de laatste stand houden, of een vaste stand


def room_temp(raw) -> int | None:
    """TemSen -> °C. De meeste modules tellen er 40 bij; nieuwere niet. Binnen is het tussen 0 en 40 °C, dus wat boven 40 ligt
    heeft de verschuiving. 0 betekent: geen sensor."""
    if not isinstance(raw, int) or isinstance(raw, bool) or raw == 0:
        return None
    return raw - 40 if raw > 40 else raw


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "airco"


class GreeHub:
    def __init__(self, data_dir: Path, engine: Engine, port: int | None = None) -> None:
        self.path = Path(data_dir) / "gree.json"
        self.engine = engine
        self.port = port  # alleen voor tests: een andere UDP-poort dan 7000
        self.changed = None  # de MQTT-brug
        self.devices: dict[str, dict] = {}
        self.clients: dict[str, GreeClient] = {}
        self.values: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.found: dict[str, dict] = {}  # mac -> {"ip", "name", "firmware", "version", "seen"}
        self._scan = None
        self.seen: dict[str, dict] = {}  # airco -> {ingang: laatst gevolgde stand}
        self._failed: dict[tuple[str, str], float] = {}  # (airco, ingang) -> wanneer het laatste bevel mislukte
        self.last: dict[str, dict] = {}  # airco -> {ingang: wat er laatst gebeurde}, voor de zijbalk
        self._load()
        engine.airco_view = self.status
        engine.airco_value = self.output

    # ------------------------------------------------------------ opslag

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in (raw.get("devices") or {}).items():
                if isinstance(v, dict) and re.fullmatch(r"[a-z0-9_]{1,40}", k):
                    self.devices[k] = self._defaults(v)
        except FileNotFoundError:
            pass
        except (ValueError, OSError, AttributeError) as e:
            log.error("%s niet leesbaar: %s", self.path, e)

    @staticmethod
    def _defaults(v: dict) -> dict:
        ins = v.get("inputs") if isinstance(v.get("inputs"), dict) else {}
        return {"name": str(v.get("name") or "Airco")[:60], "host": str(v.get("host") or ""), "mac": norm_mac(v.get("mac") or ""),
                "key": str(v.get("key") or ""), "version": v.get("version") if v.get("version") in ("auto", *VERSIONS) else "auto",
                "on_mode": v.get("on_mode") if v.get("on_mode") in ON_MODES else "keep",
                "inputs": {n: ins[n] for n, _, _ in INPUTS if isinstance(ins.get(n), str)}}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"devices": self.devices}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ toestellen

    def _check(self, dev: dict) -> None:
        if not dev["name"].strip():
            raise ModelError("geef de airco een naam")
        if dev["host"] and not HOST_RE.match(dev["host"]):
            raise ModelError("ongeldig adres: geef het IP zonder http://")
        if dev["mac"] and not MAC_RE.match(dev["mac"]):
            raise ModelError("een MAC-adres is 12 hexadecimale tekens, bv. c8f742a1b2c3")
        try:
            ok = not dev["key"] or len(dev["key"].encode("latin1")) == 16
        except UnicodeEncodeError:
            ok = False
        if not ok:
            raise ModelError("een sleutel is precies 16 tekens")

    def _inputs(self, did: str, old: dict, b) -> dict:
        if not isinstance(b, dict):
            raise ModelError("ingangen: object verwacht")
        new = dict(old)
        m = self.engine.model
        for name, want, label in INPUTS:
            if name not in b:
                continue
            ref = b[name]
            if ref in (None, ""):
                new.pop(name, None)
                continue
            if not isinstance(ref, str):
                raise ModelError(f"ingang {label}: ongeldige bron")
            got = source_type(ref, m.modules, m.timers)
            if got is None:
                raise ModelError(f"ingang {label} wijst naar iets dat niet bestaat")
            if got != want:
                raise ModelError(f"ingang {label} verwacht {'een 0 of 1' if want == BOOL else 'een getal'}" + (". Zet er een Groter dan of Kleiner dan tussen." if want == BOOL and got == NUMBER else ""))
            if REF_RE.match(ref) and REF_RE.match(ref).group(1) == did:
                raise ModelError("een airco stuurt zichzelf niet")
            new[name] = ref
        return new

    def _fields(self, did: str, dev: dict, b: dict) -> dict:
        new = {**dev, "inputs": dict(dev["inputs"])}
        for k in ("name", "host"):
            if k in b:
                new[k] = str(b[k] or "").strip()[:253]
        if "mac" in b:
            raw = str(b["mac"] or "").strip()
            if raw and not MAC_IN_RE.match(raw):
                raise ModelError("een MAC-adres is 12 hexadecimale tekens, bv. c8f742a1b2c3 of C8:F7:42:A1:B2:C3")
            new["mac"] = norm_mac(raw)
        if "version" in b:
            if b["version"] not in ("auto", *VERSIONS):
                raise ModelError("versleuteling: auto, 1 of 2")
            new["version"] = b["version"]
        if "on_mode" in b:
            if b["on_mode"] not in ON_MODES:
                raise ModelError("onbekende stand")
            new["on_mode"] = b["on_mode"]
        if b.get("key"):  # leeg bij bewerken: de sleutel blijft
            new["key"] = str(b["key"])
        if b.get("rebind"):  # opnieuw een sleutel vragen, bv. na een nieuwe wifi-koppeling van de airco
            new["key"] = ""
        elif dev["mac"] and new["mac"] != dev["mac"]:  # een andere airco: de sleutel van de vorige geldt niet
            new["key"] = str(b.get("key") or "")
        if "inputs" in b:
            new["inputs"] = self._inputs(did, dev["inputs"], b["inputs"])
        known = self.found.get(new["mac"])
        if known and not new["host"]:
            new["host"] = known["ip"]
        return new

    def add(self, b: dict) -> str:
        if len(self.devices) >= MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} airco's")
        base = did = _slug(str(b.get("name") or "airco"))
        n = 2
        while did in self.devices:
            did, n = f"{base}_{n}", n + 1
        dev = self._fields(did, self._defaults({"name": b.get("name") or "Airco"}), {k: v for k, v in b.items() if k != "inputs"})
        self._check(dev)
        self._unique(did, dev)
        self.devices[did] = dev
        self._save()
        self._start(did)
        self._poke()
        return did

    def _unique(self, did: str, dev: dict) -> None:
        if dev["mac"] and any(k != did and x["mac"] == dev["mac"] for k, x in self.devices.items()):
            raise ModelError("deze airco staat er al")

    def update(self, did: str, b: dict) -> None:
        if did not in self.devices:
            raise ModelError("onbekende airco")
        dev = self._fields(did, self.devices[did], b)
        self._check(dev)
        self._unique(did, dev)
        self._apply(did, dev)

    def _apply(self, did: str, dev: dict) -> None:
        old = self.devices[did]
        reconnect = any(dev[k] != old[k] for k in ("host", "mac", "key", "version"))
        if dev["inputs"] != old["inputs"]:
            self.seen.pop(did, None)  # een andere bron: haar eerste stand wordt weer alleen onthouden
        self.devices[did] = dev
        self._save()
        if reconnect:
            self._stop(did)
            self.values.pop(did, None)
            self._start(did)
        self._poke()

    def remove(self, did: str) -> bool:
        if did not in self.devices:
            return False
        self._stop(did)
        del self.devices[did]
        for x in (self.values, self.seen, self.last):
            x.pop(did, None)
        self._save()
        m = self.engine.model
        m.set_schema({**m.schema, "blocks": {k: v for k, v in m.schema["blocks"].items() if k != f"w:{did}"}})
        self._poke()
        return True

    # ------------------------------------------------------------ verbindingen

    def start(self) -> None:
        for did in self.devices:
            self._start(did)
        loop = asyncio.get_running_loop()
        self.tasks["_tick"] = loop.create_task(self._ticker())
        if CRYPTO:
            self.tasks["_scan"] = loop.create_task(self._scanner())

    async def _ticker(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - een fout in één ronde mag de ingangen niet voorgoed stilleggen
                log.exception("airco-ingangen")
            await asyncio.sleep(TICK)

    async def _scanner(self) -> None:
        try:
            self._scan = await scan_socket(self.on_found)
        except OSError as e:
            log.warning("zoeken naar airco's niet mogelijk: %s", e)
            return
        while True:
            if not self.devices or any(not (dev["key"] and self.is_online(did)) for did, dev in self.devices.items()):
                self.ask()
            await asyncio.sleep(SCAN_EVERY)

    def ask(self, target: str = "255.255.255.255") -> bool:
        if not self._scan:
            return False
        scan(self._scan, target, self.port or 7000)
        return True

    def on_found(self, f: dict) -> None:
        """Een airco antwoordt op de zoekvraag. Een leeg IP vullen we in; een ander IP alleen als we hem niet bereiken (DHCP)."""
        new = f["mac"] not in self.found
        self.found[f["mac"]] = {**{k: f[k] for k in ("ip", "name", "firmware", "version")}, "seen": time.time()}
        if new:
            log.info("Gree-airco %s gevonden op %s (%s)", f["mac"], f["ip"], f["name"] or "zonder naam")
        for did, dev in self.devices.items():
            if dev["mac"] == f["mac"] and dev["host"] != f["ip"] and not (dev["host"] and self.is_online(did)):
                log.info("%s gevonden op %s", dev["name"], f["ip"])
                self._apply(did, {**dev, "host": f["ip"]})
                return
        if new:
            self._poke()

    def _start(self, did: str) -> None:
        dev = self.devices[did]
        if not dev["host"]:
            return
        c = GreeClient(dev["host"], dev["mac"], dev["key"], dev["version"], port=self.port or 7000,
                       on_values=lambda v, i=did: self._on_values(i, v), on_link=lambda _up: self._poke(),
                       on_bound=lambda mac, key, ver, i=did: self._on_bound(i, mac, key, ver))
        self.clients[did] = c
        self.tasks[did] = asyncio.get_running_loop().create_task(c.run())

    def _stop(self, did: str) -> None:
        t = self.tasks.pop(did, None)
        if t:
            t.cancel()
        self.clients.pop(did, None)

    def close(self) -> None:
        for k in list(self.tasks):
            t = self.tasks.pop(k)
            t.cancel()
        self.clients.clear()
        if self._scan:
            self._scan.close()
            self._scan = None

    def _on_bound(self, did: str, mac: str, key: str, version: str) -> None:
        dev = self.devices.get(did)
        if dev:  # zonder opnieuw te verbinden: de client heeft de sleutel al
            dev.update(mac=mac, key=key, version=version)
            self._save()
            self._poke()

    def _on_values(self, did: str, vals: dict) -> None:
        dev, c = self.devices.get(did), self.clients.get(did)
        if dev is not None and c is not None and not dev["mac"] and c.mac:
            dev["mac"] = c.mac
            self._save()
        cur = self.values.setdefault(did, {})
        if any(cur.get(k) != v for k, v in vals.items()):
            cur.update(vals)
            self._poke()

    def _poke(self) -> None:
        if self.changed:
            self.changed()
        self.engine._emit("airco", log=False)

    def is_online(self, did: str) -> bool:
        c = self.clients.get(did)
        return bool(c and c.connected)

    # ------------------------------------------------------------ de stand

    def state(self, did: str) -> dict:
        v = self.values.get(did, {})
        def pick(prop, names):
            x = v.get(prop)
            return names[x] if isinstance(x, int) and 0 <= x < len(names) else None
        def flag(prop):
            return bool(v[prop]) if isinstance(v.get(prop), int) else None
        return {"power": flag("Pow"), "mode": pick("Mod", MODES), "target": v.get("SetTem") if isinstance(v.get("SetTem"), int) else None,
                "room": room_temp(v.get("TemSen")), **{k: pick(p, names) for k, (p, names) in CHOICES.items() if k != "mode"},
                **{k: flag(p) for k, (p, _) in SWITCHES.items()}}

    def output(self, ref: str):
        """De waarde op een uitgang `w:<id>:on|room|set`, of None (onbekend: offline of niet gemeld)."""
        m = REF_RE.match(ref or "")
        if not m or m.group(1) not in self.devices or not self.is_online(m.group(1)):
            return None
        s = self.state(m.group(1))
        return {"on": s["power"], "room": s["room"], "set": s["target"]}[m.group(2)]

    # ------------------------------------------------------------ bedienen

    def _values_for(self, field: str, value: str) -> dict[str, int]:
        value = str(value).strip()
        if field == "power":
            on = value.upper() in ("ON", "1", "TRUE")
            if not on and value.upper() not in ("OFF", "0", "FALSE"):
                raise ModelError(f"aan of uit, niet {value!r}")
            return {"Pow": int(on)}
        if field == "target":
            try:
                t = round(float(value))
            except (ValueError, OverflowError):
                raise ModelError("temperatuur: een getal") from None
            if not MIN_T <= t <= MAX_T:
                raise ModelError(f"temperatuur tussen {MIN_T} en {MAX_T} °C")
            return {"SetTem": t, "TemUn": 0, "TemRec": 0}
        if field in CHOICES:
            prop, names = CHOICES[field]
            if value not in names:
                raise ModelError(f"onbekende keuze {value!r}")
            return {prop: names.index(value)}
        if field in SWITCHES:
            return {SWITCHES[field][0]: self._values_for("power", value)["Pow"]}
        raise ModelError(f"onbekende instelling {field!r}")

    def _send(self, did: str, vals: dict[str, int]) -> bool:
        c = self.clients.get(did)
        return bool(c and c.set(vals))

    def set(self, did: str, field: str, value: str) -> None:
        """Eén bevel, uit HA of uit de zijbalk. De stand verandert pas als de airco antwoordt."""
        if did not in self.devices:
            raise ModelError("onbekende airco")
        if field == "mode" and value == "off":  # zo zet HA een climate uit
            vals = {"Pow": 0}
        elif field == "mode":
            vals = {"Pow": 1, **self._values_for("mode", value)}
        else:
            vals = self._values_for(field, value)
        if not self._send(did, vals):
            raise ModelError("niet verstuurd: geen verbinding met de airco")

    # ------------------------------------------------------------ de ingangen

    def _power_on(self, dev: dict) -> dict[str, int]:
        return {"Pow": 1, **({"Mod": MODES.index(dev["on_mode"])} if dev["on_mode"] in MODES else {})}

    def tick(self) -> None:
        now = time.monotonic()
        for did, dev in self.devices.items():
            mem = self.seen.setdefault(did, {})
            cur = {}
            for name, kind, _ in INPUTS:
                ref = dev["inputs"].get(name)
                v = self.engine.ref_value(ref) if ref else None
                if kind == BOOL:
                    v = v if isinstance(v, bool) else None
                else:
                    v = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
                cur[name] = v
            for name, _, label in INPUTS:
                v = cur[name]
                if v is None:
                    mem.pop(name, None)  # na onbekend telt de volgende stand weer als eerste stand
                    continue
                if name not in mem:
                    mem[name] = v
                    continue
                if v == mem[name]:
                    continue
                failed = self._failed.get((did, name))
                if failed is not None and now - failed < RETRY:  # alleen na een mislukking wachten
                    continue
                if name == "on":
                    vals = None if cur["block"] else self._power_on(dev) if v else {"Pow": 0}
                elif name == "block":
                    vals = {"Pow": 0} if v else self._power_on(dev) if cur["on"] else None
                else:
                    vals = {"SetTem": max(MIN_T, min(MAX_T, round(v))), "TemUn": 0, "TemRec": 0}
                if vals is None:
                    mem[name] = v
                    self.last.setdefault(did, {})[name] = "tegengehouden door blokkeren" if name == "on" else "niets te doen"
                    continue
                if self._send(did, vals):
                    self._failed.pop((did, name), None)
                    mem[name] = v
                    self.last.setdefault(did, {})[name] = f"{label} → {'1' if v is True else '0' if v is False else f'{v:g}'}: verstuurd"
                    self.engine._emit("airco", what="input", airco=did, input=name, value=v, log=False)
                else:
                    self._failed[(did, name)] = now
                    self.last.setdefault(did, {})[name] = f"{label}: niet verstuurd, opnieuw over {int(RETRY)} s"
                self._poke()

    # ------------------------------------------------------------ voor de pagina: nooit de sleutel

    def status(self) -> list[dict]:
        now = time.time()
        claimed = {dev["mac"] for dev in self.devices.values()}
        free = [{"mac": k, **{x: v[x] for x in ("ip", "name", "firmware", "version")}} for k, v in sorted(self.found.items(), key=lambda kv: kv[1]["ip"])
                if k not in claimed and now - v["seen"] < FOUND_KEEP]
        out = []
        for did, dev in self.devices.items():
            c = self.clients.get(did)
            out.append({"id": did, "name": dev["name"], "host": dev["host"], "mac": dev["mac"] or (c.mac if c else ""), "version": (c.version if c else dev["version"]),
                        "firmware": (c.firmware if c else "") or (self.found.get(dev["mac"]) or {}).get("firmware", ""), "has_key": bool(dev["key"]),
                        "configured": bool(dev["host"]), "online": self.is_online(did), "error": c.last_error if c else "", "available": CRYPTO,
                        "on_mode": dev["on_mode"], "inputs": dict(dev["inputs"]), "last": dict(self.last.get(did, {})), "state": self.state(did),
                        "outputs": {n: self.output(f"w:{did}:{n}") for n, _, _ in OUTPUTS}, "unclaimed": free, "scanning": bool(self._scan)})
        return out

    # ------------------------------------------------------------ MQTT

    @staticmethod
    def t(did: str, what: str, kind: str = "state") -> str:
        return f"{d.PREFIX}/gree/{did}/{what}/{kind}"

    @property
    def filters(self) -> list[str]:
        return [f"{d.PREFIX}/gree/+/+/set"]

    def discovery(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for did, dev in self.devices.items():
            device = {"identifiers": [f"kk_gree_{did}"], "name": dev["name"], "manufacturer": "Gree", "model": "Airco (Huisconfig)"}
            base = {"device": device, "has_entity_name": True, "availability_mode": "all", "availability": [{"topic": d.BRIDGE_STATUS}, {"topic": self.t(did, "online")}]}
            oid = f"kk_gree_{did}"
            out[f"{d.DISCOVERY}/climate/{oid}/config"] = {
                **base, "name": None, "unique_id": oid, "default_entity_id": f"climate.{oid}",
                "modes": ["off", *MODES], "mode_state_topic": self.t(did, "mode"), "mode_command_topic": self.t(did, "mode", "set"),
                "temperature_state_topic": self.t(did, "target"), "temperature_command_topic": self.t(did, "target", "set"),
                "current_temperature_topic": self.t(did, "room"), "min_temp": MIN_T, "max_temp": MAX_T, "temp_step": 1, "precision": 1.0, "temperature_unit": "C",
                "fan_modes": FANS, "fan_mode_state_topic": self.t(did, "fan"), "fan_mode_command_topic": self.t(did, "fan", "set"),
                "swing_modes": SWING_V, "swing_mode_state_topic": self.t(did, "swing_v"), "swing_mode_command_topic": self.t(did, "swing_v", "set"),
                "swing_horizontal_modes": SWING_H, "swing_horizontal_mode_state_topic": self.t(did, "swing_h"), "swing_horizontal_mode_command_topic": self.t(did, "swing_h", "set")}
            out[f"{d.DISCOVERY}/sensor/{oid}_room/config"] = {
                **base, "name": "Kamertemperatuur", "unique_id": f"{oid}_room", "default_entity_id": f"sensor.{oid}_room", "state_topic": self.t(did, "room"),
                "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"}
            for k, (_, name) in SWITCHES.items():
                out[f"{d.DISCOVERY}/switch/{oid}_{k}/config"] = {
                    **base, "name": name, "unique_id": f"{oid}_{k}", "default_entity_id": f"switch.{oid}_{k}", "state_topic": self.t(did, k),
                    "command_topic": self.t(did, k, "set"), "payload_on": "ON", "payload_off": "OFF", "entity_category": "config"}
        return out

    def states(self) -> dict[str, tuple[str, bool]]:
        out: dict[str, tuple[str, bool]] = {}
        for did in self.devices:
            out[self.t(did, "online")] = ("online" if self.is_online(did) else "offline", True)
            s = self.state(did)
            if s["power"] is not None:
                out[self.t(did, "mode")] = ((s["mode"] or "auto") if s["power"] else "off", True)
            for k in ("target", "room", "fan", "swing_v", "swing_h"):
                if s[k] is not None:
                    out[self.t(did, k)] = (str(s[k]), True)
            for k in SWITCHES:
                if s[k] is not None:
                    out[self.t(did, k)] = ("ON" if s[k] else "OFF", True)
        return out

    def command(self, topic: str, payload: str) -> bool:
        m = SET_RE.match(topic)
        if not m:
            return False
        try:
            self.set(m.group(1), m.group(2), payload)
        except ModelError as e:
            log.warning("airco %s %s: %s", m.group(1), m.group(2), e)
        return True
