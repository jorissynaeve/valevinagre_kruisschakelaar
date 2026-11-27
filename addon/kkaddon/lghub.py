"""LG-tv's met webOS rechtstreeks over het LAN, als eigen blok `l:<id>` (de tv als huis-screensaver, zie TV-OVERDRACHT.md).

Zoals de airco (greehub.py) heeft het blok eigen poorten:

  links   aan (0/1), dashboard (0/1)        een timer, module of waarde stuurt de tv
  rechts  aan (0/1), wordt bekeken (0/1)    `l:<id>:on|watch`, voor de logica en de routines

  aan        1 zet de tv aan (Wake-on-LAN als hij uit is, het scherm aan als alleen het scherm uit was), 0 zet hem uit.
  dashboard  1 zet de tv aan en opent het adres van het dashboard in de browser van de tv; 0 haalt het weg (scherm uit, tv uit
             of terug naar het beginscherm, naar keuze) maar **nooit terwijl er gekeken wordt**: dan blijft de tv zoals hij is.
  aan (uit)  is 1 als de tv wakker is met het scherm aan; een tv die uit staat of niet bereikbaar is, geeft 0.
  bekeken    is 1 als hij aan is en er een app vooraan staat die niet in de lijst *eigen apps* staat (browser, screensaver, begin).

Een ingang handelt op een **wijziging**, nooit op een stand (zoals greehub.py): de eerste stand na een herstart of na onbekend
wordt alleen onthouden. Een tv die uit staat luistert niet; aanzetten voor het dashboard wacht daarom tot hij bereikbaar is.

De sleutel van de koppeling staat in /data/lgtv.json (0600), niet in het model, en nooit in de status voor de pagina.
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
from .lg import LgClient, norm_mac, wake
from .logic import BOOL
from .model import ModelError, fits, source_type

log = logging.getLogger("kk.lg")

MAX_DEVICES = 8
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
MAC_RE = re.compile(r"^[0-9a-f]{12}$")
APP_RE = re.compile(r"^[A-Za-z0-9._\-]{1,100}$")
URL_RE = re.compile(r"^https?://[^\s]{1,500}$")
REF_RE = re.compile(r"^l:([a-z0-9_]{1,40}):(on|watch)$")
TICK = 1.0  # s
WAKE_EVERY = 15.0  # s: zo vaak een Wake-on-LAN-pakket zolang de tv niet reageert
SHOW_FOR = 180.0  # s: zo lang wachten tot een tv na aanzetten bereikbaar is voor het dashboard
RETRY = 30.0  # s: een ingang die niet doorkwam, opnieuw proberen
HOME = "com.webos.app.home"
OWN_APPS = ["com.webos.app.browser", "com.webos.app.screensaver", HOME]  # wat niet als "kijken" telt
OFF_MODES = {"screen": "scherm uit", "power": "tv uit", "home": "terug naar het beginscherm"}
# de ingangen en uitgangen: (naam, soort, label)
INPUTS = (("on", BOOL, "aan"), ("show", BOOL, "dashboard"))
OUTPUTS = (("on", BOOL, "aan"), ("watch", BOOL, "wordt bekeken"))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "tv"


class LgHub:
    def __init__(self, data_dir: Path, engine: Engine, port: int | None = None, scheme: str | None = None) -> None:
        self.path = Path(data_dir) / "lgtv.json"
        self.engine = engine
        self.port, self.scheme = port, scheme  # alleen voor tests: een andere poort dan 3001/3000
        self.changed = None  # de MQTT-brug
        self.devices: dict[str, dict] = {}
        self.clients: dict[str, LgClient] = {}
        self.values: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.seen: dict[str, dict] = {}  # tv -> {ingang: laatst gevolgde stand}
        self.pending: dict[str, dict] = {}  # tv -> {"until", "woke"}: het dashboard moet nog open
        self.last: dict[str, dict] = {}  # tv -> {ingang: wat er laatst gebeurde}, voor de zijbalk
        self._failed: dict[tuple[str, str], float] = {}
        self._load()
        engine.lg_view = self.status
        engine.lg_value = self.output

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
        apps = v.get("apps") if isinstance(v.get("apps"), list) else OWN_APPS
        return {"name": str(v.get("name") or "TV")[:60], "host": str(v.get("host") or ""), "mac": norm_mac(v.get("mac") or ""), "key": str(v.get("key") or ""),
                "url": str(v.get("url") or ""), "off_mode": v.get("off_mode") if v.get("off_mode") in OFF_MODES else "screen",
                "apps": [a for a in apps if isinstance(a, str) and APP_RE.match(a)][:40],
                "inputs": {n: ins[n] for n, _, _ in INPUTS if isinstance(ins.get(n), str)}}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"devices": self.devices}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ toestellen

    @staticmethod
    def _check(dev: dict) -> None:
        if not dev["name"].strip():
            raise ModelError("geef de tv een naam")
        if dev["host"] and not HOST_RE.match(dev["host"]):
            raise ModelError("ongeldig adres: geef het IP zonder http://")
        if dev["mac"] and not MAC_RE.match(dev["mac"]):
            raise ModelError("een MAC-adres is 12 hexadecimale tekens, bv. c8f742a1b2c3")
        if dev["url"] and not URL_RE.match(dev["url"]):
            raise ModelError("het adres van het dashboard begint met http:// of https://")

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
            if not fits(got, want):
                raise ModelError(f"ingang {label} verwacht een 0 of 1. Zet er een Groter dan of Kleiner dan tussen.")
            hit = REF_RE.match(ref)
            if hit and hit.group(1) == did:
                raise ModelError("een tv stuurt zichzelf niet")
            new[name] = ref
        return new

    def _fields(self, did: str, dev: dict, b: dict) -> dict:
        new = {**dev, "inputs": dict(dev["inputs"])}
        for k in ("name", "host", "url"):
            if k in b:
                new[k] = str(b[k] or "").strip()[:500 if k == "url" else 253]
        if "mac" in b:
            raw = str(b["mac"] or "").strip()
            if raw and not MAC_RE.match(norm_mac(raw)):
                raise ModelError("een MAC-adres is 12 hexadecimale tekens, bv. c8f742a1b2c3 of C8:F7:42:A1:B2:C3")
            new["mac"] = norm_mac(raw)
        if "off_mode" in b:
            if b["off_mode"] not in OFF_MODES:
                raise ModelError("onbekende keuze voor het weghalen van het dashboard")
            new["off_mode"] = b["off_mode"]
        if "apps" in b:
            apps = b["apps"] if isinstance(b["apps"], list) else str(b["apps"] or "").replace(",", " ").split()
            if not all(isinstance(a, str) and APP_RE.match(a) for a in apps) or len(apps) > 40:
                raise ModelError("eigen apps: app-id's zoals com.webos.app.browser, gescheiden door spaties")
            new["apps"] = list(dict.fromkeys(apps))
        if b.get("rebind") or (dev["host"] and "host" in b and new["host"] != dev["host"]):  # een andere tv of opnieuw koppelen: de oude sleutel geldt niet
            new["key"] = ""
        if "inputs" in b:
            new["inputs"] = self._inputs(did, dev["inputs"], b["inputs"])
        return new

    def add(self, b: dict) -> str:
        if len(self.devices) >= MAX_DEVICES:
            raise ModelError(f"maximaal {MAX_DEVICES} tv's")
        base = did = _slug(str(b.get("name") or "tv"))
        n = 2
        while did in self.devices:
            did, n = f"{base}_{n}", n + 1
        dev = self._fields(did, self._defaults({"name": b.get("name") or "TV"}), {k: v for k, v in b.items() if k not in ("inputs", "key", "rebind")})
        self._check(dev)
        self.devices[did] = dev
        self._save()
        self._start(did)
        self._poke()
        return did

    def update(self, did: str, b: dict) -> None:
        if did not in self.devices:
            raise ModelError("onbekende tv")
        dev = self._fields(did, self.devices[did], {k: v for k, v in b.items() if k != "key"})
        self._check(dev)
        old = self.devices[did]
        reconnect = dev["host"] != old["host"] or dev["key"] != old["key"]
        if dev["inputs"] != old["inputs"]:
            self.seen.pop(did, None)  # een andere bron: haar eerste stand wordt weer alleen onthouden
        self.devices[did] = dev
        self._save()
        if reconnect:
            self._stop(did)
            self.values.pop(did, None)
            self._start(did)
        self._poke()

    def pair(self, did: str) -> None:
        """Opnieuw koppelen (na een weigering): de tv toont de melding weer."""
        if did not in self.devices:
            raise ModelError("onbekende tv")
        if not self.devices[did]["host"]:
            raise ModelError("vul eerst het adres van de tv in")
        self._stop(did)
        self._start(did)
        self._poke()

    def remove(self, did: str) -> bool:
        if did not in self.devices:
            return False
        self._stop(did)
        del self.devices[did]
        for x in (self.values, self.seen, self.last, self.pending):
            x.pop(did, None)
        self._save()
        m = self.engine.model
        m.set_schema({**m.schema, "blocks": {k: v for k, v in m.schema["blocks"].items() if k != f"l:{did}"}})
        self._poke()
        return True

    # ------------------------------------------------------------ verbindingen

    def start(self) -> None:
        for did in self.devices:
            self._start(did)
        self.tasks["_tick"] = asyncio.get_running_loop().create_task(self._ticker())

    async def _ticker(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - een fout in één ronde mag de ingangen niet voorgoed stilleggen
                log.exception("tv-ingangen")
            await asyncio.sleep(TICK)

    def _start(self, did: str) -> None:
        dev = self.devices[did]
        if not dev["host"]:
            return
        c = LgClient(dev["host"], dev["key"], port=self.port, scheme=self.scheme,
                     on_state=lambda v, i=did: self._on_state(i, v), on_link=lambda _up, i=did: self._on_link(i),
                     on_key=lambda key, i=did: self._on_key(i, key))
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

    def _on_key(self, did: str, key: str) -> None:
        dev = self.devices.get(did)
        if dev:  # zonder opnieuw te verbinden: de client heeft de sleutel al
            dev["key"] = key
            self._save()
            self._poke()

    def _on_link(self, did: str) -> None:
        c = self.clients.get(did)
        if c is not None and not c.connected:
            self.values.pop(did, None)  # wat de tv meldde, geldt niet meer
        self._poke()

    def _on_state(self, did: str, vals: dict) -> None:
        cur = self.values.setdefault(did, {})
        if any(cur.get(k) != v for k, v in vals.items()):
            cur.update(vals)
            self._poke()

    def _poke(self) -> None:
        if self.changed:
            self.changed()
        self.engine._emit("lg", log=False)

    def is_online(self, did: str) -> bool:
        c = self.clients.get(did)
        return bool(c and c.connected)

    # ------------------------------------------------------------ de stand

    def state(self, did: str) -> dict:
        """`on`: wakker met het scherm aan; `watching`: aan met een app die niet eigen is. None als we het niet weten (nog niet gekoppeld)."""
        dev, v, c = self.devices[did], self.values.get(did, {}), self.clients.get(did)
        known = bool(dev["key"]) or bool(c and c.connected)
        if c is not None and c.connected:
            on = v.get("power") in (None, "Active")
        else:
            on = False if known else None  # een tv die uit staat luistert niet meer: dat is "uit", niet "onbekend"
        app = v.get("app") if on else None
        watching = None if on is None else bool(on and app and app not in dev["apps"])
        return {"on": on, "screen_off": v.get("power") == "Screen Off", "watching": watching, "app": v.get("app"), "power": v.get("power"),
                "volume": v.get("volume"), "muted": v.get("muted")}

    def output(self, ref: str):
        """De waarde op een uitgang `l:<id>:on|watch`, of None (onbekend: nog niet gekoppeld, of de tv bestaat niet)."""
        m = REF_RE.match(ref or "")
        if not m or m.group(1) not in self.devices:
            return None
        s = self.state(m.group(1))
        return {"on": s["on"], "watch": s["watching"]}[m.group(2)]

    # ------------------------------------------------------------ bedienen

    def _client(self, did: str) -> LgClient:
        if did not in self.devices:
            raise ModelError("onbekende tv")
        c = self.clients.get(did)
        if c is None or not c.connected:
            raise ModelError("niet verstuurd: geen verbinding met de tv")
        return c

    def power(self, did: str, on: bool) -> None:
        """Eén bevel, uit de zijbalk: aan is Wake-on-LAN als de tv niet bereikbaar is, of het scherm aan als alleen dat uit was."""
        if on:
            if not self._power_up(did):
                raise ModelError("niet verstuurd: geen verbinding en geen MAC-adres voor Wake-on-LAN")
        elif not self._client(did).power_off():
            raise ModelError("niet verstuurd: geen verbinding met de tv")

    def screen(self, did: str, on: bool) -> None:
        if not self._client(did).screen(on):
            raise ModelError("niet verstuurd: geen verbinding met de tv")

    def show(self, did: str) -> None:
        """Het dashboard nu openen, zonder te wachten op de ingang (de knop in de zijbalk)."""
        if did not in self.devices:
            raise ModelError("onbekende tv")
        if not self.devices[did]["url"]:
            raise ModelError("vul eerst het adres van het dashboard in")
        self.pending[did] = {"until": time.monotonic() + SHOW_FOR, "woke": 0.0}
        self.tick()

    def _power_up(self, dev_id: str) -> bool:
        dev, c = self.devices[dev_id], self.clients.get(dev_id)
        if c is not None and c.connected:
            return c.screen(True) if self.state(dev_id)["screen_off"] else True  # wakker: alleen het scherm kan nog uit zijn
        return bool(dev["mac"]) and wake(dev["mac"])

    # ------------------------------------------------------------ de ingangen

    def _hide(self, did: str) -> str:
        """Het dashboard weghalen, behalve als er gekeken wordt. Geeft wat er gebeurde."""
        dev, s = self.devices[did], self.state(did)
        self.pending.pop(did, None)
        if not s["on"]:
            return "de tv is al uit"
        if s["watching"]:
            return "niet weggehaald: de tv wordt bekeken"
        c = self.clients[did]
        sent = {"screen": lambda: c.screen(False), "power": c.power_off, "home": lambda: c.launch(HOME)}[dev["off_mode"]]()
        return OFF_MODES[dev["off_mode"]] if sent else ""

    def _step_show(self, did: str, now: float) -> None:
        """Eén stap naar een open dashboard: wakker maken, wachten tot de tv bereikbaar is, het adres openen."""
        p, dev, c = self.pending[did], self.devices[did], self.clients.get(did)
        if now > p["until"]:
            self.pending.pop(did)
            self.last.setdefault(did, {})["show"] = "dashboard: de tv werd niet bereikbaar, opgegeven"
            self._poke()
            return
        if c is None or not c.connected or c.pairing:
            if dev["mac"] and now - p["woke"] >= WAKE_EVERY:
                p["woke"] = now
                wake(dev["mac"])
            return
        s = self.state(did)
        if s["screen_off"]:
            c.screen(True)
            return
        if s["on"] and c.open_url(dev["url"]):
            self.pending.pop(did)
            self.last.setdefault(did, {})["show"] = "dashboard: geopend"
            self.engine._emit("lg", what="show", lg=did, log=False)
            self._poke()

    def tick(self) -> None:
        now = time.monotonic()
        for did in list(self.pending):
            self._step_show(did, now)
        for did, dev in self.devices.items():
            mem = self.seen.setdefault(did, {})
            for name, _, label in INPUTS:
                ref = dev["inputs"].get(name)
                v = self.engine.ref_value(ref) if ref else None
                v = v if isinstance(v, bool) else None
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
                if name == "show" and v and not dev["url"]:
                    mem[name] = v
                    self.last.setdefault(did, {})[name] = "dashboard: vul eerst het adres in"
                    self._poke()
                    continue
                msg = self._apply_input(did, dev, name, v, now)
                if msg is None:
                    self._failed[(did, name)] = now
                    self.last.setdefault(did, {})[name] = f"{label}: niet verstuurd, opnieuw over {int(RETRY)} s"
                else:
                    self._failed.pop((did, name), None)
                    mem[name] = v
                    self.last.setdefault(did, {})[name] = f"{label} → {'1' if v else '0'}: {msg}"
                    self.engine._emit("lg", what="input", lg=did, input=name, value=v, log=False)
                self._poke()

    def _apply_input(self, did: str, dev: dict, name: str, v: bool, now: float) -> str | None:
        """Voert een gewijzigde ingang uit. Geeft een tekst voor de zijbalk, of None als het niet lukte (dan later opnieuw)."""
        c = self.clients.get(did)
        if name == "on":
            if v:
                return "verstuurd" if self._power_up(did) else None
            if c is None or not c.connected:
                return "de tv is al uit"
            return "verstuurd" if c.power_off() else None
        if v:  # dashboard: de stappen lopen in de tick, ook als de tv eerst wakker moet worden
            self.pending[did] = {"until": now + SHOW_FOR, "woke": 0.0}
            self._step_show(did, now)
            return "wordt geopend"
        msg = self._hide(did)
        return msg or None

    # ------------------------------------------------------------ voor de pagina: nooit de sleutel

    def status(self) -> list[dict]:
        out = []
        for did, dev in self.devices.items():
            c = self.clients.get(did)
            out.append({"id": did, "name": dev["name"], "host": dev["host"], "mac": dev["mac"], "url": dev["url"], "off_mode": dev["off_mode"],
                        "apps": list(dev["apps"]), "has_key": bool(dev["key"]), "configured": bool(dev["host"]), "online": self.is_online(did),
                        "pairing": bool(c and c.pairing), "rejected": bool(c and c.rejected), "error": c.last_error if c else "",
                        "showing": did in self.pending, "inputs": dict(dev["inputs"]), "last": dict(self.last.get(did, {})), "state": self.state(did),
                        "outputs": {n: self.output(f"l:{did}:{n}") for n, _, _ in OUTPUTS}})
        return out

    # ------------------------------------------------------------ MQTT

    @staticmethod
    def t(did: str, what: str) -> str:
        return f"{d.PREFIX}/lgtv/{did}/{what}/state"

    @property
    def filters(self) -> list[str]:
        return []  # alleen standen naar HA; bedienen gaat via het schema

    def discovery(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for did, dev in self.devices.items():
            device = {"identifiers": [f"kk_lgtv_{did}"], "name": dev["name"], "manufacturer": "LG", "model": "webOS-tv (Huisconfig)"}
            base = {"device": device, "has_entity_name": True, "availability_mode": "all", "availability": [{"topic": d.BRIDGE_STATUS}]}
            for k, name, cls in (("on", "Aan", "power"), ("watch", "Wordt bekeken", None)):
                oid = f"kk_lgtv_{did}_{k}"
                out[f"{d.DISCOVERY}/binary_sensor/{oid}/config"] = {
                    **base, "name": name, "unique_id": oid, "default_entity_id": f"binary_sensor.{oid}", "state_topic": self.t(did, k),
                    "payload_on": "ON", "payload_off": "OFF", **({"device_class": cls} if cls else {})}
        return out

    def states(self) -> dict[str, tuple[str, bool]]:
        out: dict[str, tuple[str, bool]] = {}
        for did in self.devices:
            for k, n in (("on", "on"), ("watch", "watching")):
                val = self.state(did)[n]
                if val is not None:
                    out[self.t(did, k)] = ("ON" if val else "OFF", True)
        return out

    def command(self, topic: str, payload: str) -> bool:
        return False
