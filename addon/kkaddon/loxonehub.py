"""De brug naar een Loxone Miniserver (vervangt JavaLox): signalen tussen punten van de add-on en Loxone, over UDP en HTTP. Zie DESIGN.md §40.

Een **signaal** koppelt een bron aan een naam in een bericht (`kk.<id>=<getal>`, loxone.py):
- `out`: de add-on leest een uitgang (`e:<apparaat>:<punt>` van een HUISDEV-apparaat, een timer of module, of `online:<apparaat>`) en stuurt de waarde als UDP naar de
  virtuele UDP-ingang van de Miniserver. Bij een verandering, elke minuut opnieuw (de Miniserver verliest zijn ingangen bij een herstart, UDP is niet gegarandeerd) en
  10 s na het starten (een volledige stand, uitgespreid). Een onbekende waarde (apparaat offline) wordt niet verstuurd: een verzonnen 0 is gevaarlijker dan niets.
- `in` met een actie (`a:<slot>:<actie>`): Loxone start een actie van een Nuki-slot (vergrendelen, ontgrendelen). Strenger dan een punt, want het is een deur: zie `_apply_action`.
- `in`: de Miniserver zet een punt van een HUISDEV-apparaat, over UDP of HTTP (GET/POST naar `/loxone`). Alleen absolute waarden, alleen van het IP van de Miniserver,
  alleen punten die instelbaar zijn en niet `danger` (een deur alleen via een actie, zie hieronder), binnen bereik en stap van het punt, en alleen als de waarde verandert.

De Miniserver is een eerste generatie (waarschijnlijk): hij verwerkt UDP niet in een vloed. Daarom gaat er hoogstens één bericht per `GAP` de lijn op en nooit twee keer
hetzelfde signaal binnen `MIN_GAP`.

De signalen staan in /data/loxone.json (0600), samen met het adres van de Miniserver. Het sjabloon voor Loxone Config maakt loxone.py.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
from collections import OrderedDict, deque
from pathlib import Path

from aiohttp import web

from . import loxone as lx
from .espdevhub import ACTION_GAP, REF_RE, _snap
from .model import SOURCE_RE, ModelError

log = logging.getLogger("kk.loxone")

TICK = 1.0  # s: hoe vaak de uitgangen worden gelezen
GAP = 0.05  # s tussen twee berichten naar de Miniserver
MIN_GAP = 1.0  # s: hetzelfde signaal hoogstens zo vaak, ook bij een snel veranderende waarde
START_DELAY = 10.0  # s na het starten tot de volledige stand wordt gestuurd
HEARTBEAT = 60  # s: een signaal zonder verandering wordt zo vaak herhaald
MAX_SIGNALS = 300
DEFAULT_IN_PORT = 47510  # waar de add-on luistert (UDP en HTTP)
DEFAULT_OUT_PORT = 9100  # waar de Miniserver luistert (zijn virtuele UDP-ingang)
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
ONLINE_RE = re.compile(r"^online:([a-z0-9_]{1,40})$")
ACT_RE = re.compile(r"^a:([a-z0-9_]{1,40}):([a-z][a-z0-9_]{0,23})$")  # een actie van een Nuki-slot: `a:<apparaat>:<actie>`
KINDS = ("bool", "int", "float")
NO_DEVICE = "-"  # de groep van signalen zonder apparaat (een timer of module als bron)
MAIN = "main"  # het standaardkanaal: zijn poort is `out_port`
TRAFFIC = 150  # zoveel berichten blijven in het verkeerslogboek
MAX_CHANNELS = 20  # een Miniserver kent hoogstens ~50 verschillende UDP-poorten voor virtuele ingangen


def _int(raw, lo: int, hi: int, what: str) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ModelError(f"{what}: een getal") from None
    if isinstance(raw, bool) or not lo <= v <= hi:
        raise ModelError(f"{what}: tussen {lo} en {hi}")
    return v


def _ip(raw, what: str) -> str:
    s = str(raw or "").strip()
    if s:
        try:
            ipaddress.IPv4Address(s)
        except ValueError:
            raise ModelError(f"{what}: een IPv4-adres, bv. 192.168.1.112") from None
    return s


def local_ip(target: str) -> str:
    """Het eigen IP op de weg naar `target` (er gaat niets over de lijn), of ''."""
    if not target:
        return ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target, 9))
            return s.getsockname()[0]
    except OSError:
        return ""


class LoxoneHub:
    def __init__(self, data_dir: Path, engine, espdev=None, port: int | None = None, clock=None, gap: float | None = None, start_delay: float | None = None,
                 tick: float | None = None) -> None:
        self.path = Path(data_dir) / "loxone.json"
        self.engine = engine
        self.espdev = espdev
        self.port = DEFAULT_IN_PORT if port is None else port  # 0: een vrije poort (tests)
        self.clock = clock or time.monotonic
        self.gap = GAP if gap is None else gap
        self.start_delay = START_DELAY if start_delay is None else start_delay
        self.tick_every = TICK if tick is None else tick
        self.miniserver = ""
        self.out_port = DEFAULT_OUT_PORT
        self.addon_host = ""
        self.transport = "http"  # hoe de Miniserver de add-on aanroept: http of udp
        self.signals: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}  # extra kanalen: id -> {"name", "port"}; het standaardkanaal (`MAIN`) staat hier niet in
        self.assign: dict[str, str] = {}  # apparaat (of `-`) -> kanaal; wat er niet in staat zit in het standaardkanaal
        self.sent: dict[str, dict] = {}  # id -> {"txt", "v", "t", "wall"} van het laatst verstuurde
        self.got: dict[str, dict] = {}  # id -> {"v", "wall", "result"} van het laatst ontvangene
        self.queue: OrderedDict[str, bytes] = OrderedDict()
        self.act_mem: dict[str, bool] = {}  # de laatst ontvangen waarde van een actie-signaal sinds het starten (voor de overgang 0 naar 1)
        self.act_last: dict[str, float] = {}  # wanneer een actie-signaal voor het laatst iets startte
        self.traffic: deque = deque(maxlen=TRAFFIC)  # de laatste berichten, in en uit, om te zien wat er echt over de lijn ging
        self.seen: set[str] = set()  # ingaande signalen waarvan sinds het starten al een waarde kwam (voor skip_first)
        self.stats = {"sent": 0, "received": 0, "refused": 0, "unknown": 0}
        self.changed = None
        self._t0 = self.clock()
        self._udp = None
        self._runner: web.AppRunner | None = None
        self._tasks: list[asyncio.Task] = []
        self.udp_port = 0  # de poorten waarop we echt luisteren
        self.http_port = 0
        self.error = ""
        self._warned = False
        self._load()

    # ------------------------------------------------------------ opslag

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError) as e:
            log.error("%s niet leesbaar: %s", self.path, e)
            return
        try:
            self.miniserver = _ip(raw.get("miniserver"), "Miniserver")
            self.out_port = _int(raw.get("out_port", DEFAULT_OUT_PORT), 1, 65535, "poort")
            self.addon_host = self._host(raw.get("addon_host"))
            self.transport = raw.get("transport") if raw.get("transport") in ("http", "udp") else "http"
        except ModelError as e:
            log.error("instellingen in %s ongeldig: %s", self.path, e)
        for cid, c in (raw.get("channels") or {}).items():
            try:
                self._check_channel(cid, c.get("name"), c.get("port"))
                self.channels[cid] = {"name": str(c["name"]).strip()[:30], "port": c["port"]}
            except (ModelError, AttributeError) as e:
                log.error("kanaal %s in %s overgeslagen: %s", cid, self.path, e)
        for g, cid in (raw.get("assign") or {}).items():
            if isinstance(g, str) and cid in self.channels:
                self.assign[g] = cid
        for sid, s in (raw.get("signals") or {}).items():
            try:
                self.signals[sid] = self._clean(sid, s)
            except ModelError as e:
                log.error("signaal %s in %s overgeslagen: %s", sid, self.path, e)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"miniserver": self.miniserver, "out_port": self.out_port, "addon_host": self.addon_host, "transport": self.transport, "signals": self.signals,
                       "channels": self.channels, "assign": self.assign},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _poke(self) -> None:
        if self.changed:
            self.changed()
        self.engine._emit("loxone", log=False)

    # ------------------------------------------------------------ instellingen

    @staticmethod
    def _host(raw) -> str:
        s = str(raw or "").strip()
        if s and not HOST_RE.match(s):
            raise ModelError("adres van de add-on: een IP-adres of hostnaam")
        return s

    def configure(self, b: dict) -> None:
        mini = _ip(b["miniserver"], "Miniserver") if "miniserver" in b else self.miniserver
        port = _int(b["out_port"], 1, 65535, "poort van de Miniserver") if "out_port" in b else self.out_port
        host = self._host(b["addon_host"]) if "addon_host" in b else self.addon_host
        transport = b.get("transport", self.transport)
        if transport not in ("http", "udp"):
            raise ModelError("verbinding naar de add-on: http of udp")
        if any(c["port"] == port for c in self.channels.values()):
            raise ModelError(f"poort {port} is al van een ander kanaal")
        if transport == "udp" and any(self.is_danger(sg) for sg in self.signals.values()):
            raise ModelError("er zijn gevaarlijke acties (ontgrendelen): die worden alleen over HTTP aangenomen. Haal ze eerst weg, of kies HTTP")
        self.miniserver, self.out_port, self.addon_host, self.transport = mini, port, host, transport
        self._save()
        self._poke()

    # ------------------------------------------------------------ kanalen

    # Een kanaal is één virtuele UDP-ingang plus één virtuele uitgang in Loxone, met een eigen poort voor de ingang. Een sjabloon dat je al in Config hebt, werkt Config niet bij
    # en twee virtuele UDP-ingangen op dezelfde poort kunnen niet: een nieuw apparaat in een nieuw kanaal is dus een nieuw bestand, zonder iets te veranderen aan wat er staat.

    def _check_channel(self, cid, name, port, current: str | None = None) -> None:
        if not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9_]{1,20}", cid) or cid == MAIN:
            raise ModelError("onbruikbaar kanaal")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 30:
            raise ModelError("naam van het kanaal: 1 tot 30 tekens")
        port = _int(port, 1, 65535, "poort van het kanaal")
        if port == self.out_port or any(c["port"] == port for k, c in self.channels.items() if k != current):
            raise ModelError(f"poort {port} is al van een ander kanaal")

    def channel_list(self) -> list[dict]:
        return [{"id": MAIN, "name": "Standaard", "port": self.out_port, "default": True}] + [
            {"id": k, "name": c["name"], "port": c["port"], "default": False} for k, c in sorted(self.channels.items(), key=lambda kv: kv[1]["name"].lower())]

    def channel_of(self, s: dict) -> str:
        cid = self.assign.get(self.group_of(s), MAIN)
        return cid if cid in self.channels else MAIN

    def channel_port(self, cid: str) -> int:
        return self.out_port if cid == MAIN or cid not in self.channels else self.channels[cid]["port"]

    def channel_name(self, cid: str) -> str:
        return "Standaard" if cid == MAIN or cid not in self.channels else self.channels[cid]["name"]

    def add_channel(self, name: str, port) -> str:
        if len(self.channels) >= MAX_CHANNELS:
            raise ModelError(f"hoogstens {MAX_CHANNELS} kanalen")
        name = str(name or "").strip()
        base = re.sub(r"[^a-z0-9]+", "_", name.lower().encode("ascii", "ignore").decode()).strip("_")[:16] or "kanaal"
        cid, n = base, 2
        while cid in self.channels or cid == MAIN:
            cid, n = f"{base}_{n}", n + 1
        self._check_channel(cid, name, port)
        self.channels[cid] = {"name": name[:30], "port": _int(port, 1, 65535, "poort van het kanaal")}
        self._save()
        self._poke()
        return cid

    def update_channel(self, cid: str, b: dict) -> None:
        c = self.channels.get(cid)
        if not c:
            raise ModelError("onbekend kanaal (de poort van het standaardkanaal stel je in bij de verbinding)")
        name, port = b.get("name", c["name"]), b.get("port", c["port"])
        self._check_channel(cid, name, port, current=cid)
        moved = _int(port, 1, 65535, "poort van het kanaal") != c["port"]
        c["name"], c["port"] = str(name).strip()[:30], _int(port, 1, 65535, "poort van het kanaal")
        self._save()
        if moved:
            self.request_snapshot()  # de ingang staat nu op een andere poort: alles opnieuw, daar naartoe
        self._poke()

    def remove_channel(self, cid: str) -> bool:
        if cid not in self.channels:
            return False
        using = sorted({self.group_name(g) for g, c in self.assign.items() if c == cid and any(self.group_of(s) == g for s in self.signals.values())})
        if using:
            raise ModelError("dit kanaal wordt gebruikt door " + ", ".join(using) + ": zet die eerst in een ander kanaal")
        del self.channels[cid]
        self.assign = {g: c for g, c in self.assign.items() if c != cid}
        self._save()
        self._poke()
        return True

    def set_channel(self, group: str, cid: str) -> None:
        """Alle signalen van een apparaat (`group`) in een ander kanaal. Wat naar Loxone gaat, gaat dan naar de poort van dat kanaal."""
        if not any(self.group_of(s) == group for s in self.signals.values()):
            raise ModelError("onbekend apparaat")
        if cid != MAIN and cid not in self.channels:
            raise ModelError("onbekend kanaal")
        if cid == MAIN:
            self.assign.pop(group, None)
        else:
            self.assign[group] = cid
        self._save()
        self.request_snapshot()
        self._poke()

    # ------------------------------------------------------------ signalen

    def _point(self, ref: str):
        """(apparaat, punt) van een `e:<apparaat>:<punt>`, of (None, None)."""
        m = REF_RE.match(ref or "")
        dev = self.espdev.devices.get(m.group(1)) if m and self.espdev else None
        return (dev, next((p for p in dev["points"] if p["id"] == m.group(2)), None)) if dev else (None, None)

    def _action(self, ref: str):
        """(slot, actie) van een `a:<slot>:<actie>`, of (None, None). Alleen een actie zonder argumenten van een Nuki-slot: dat heeft een 0/1."""
        m = ACT_RE.match(ref or "")
        dev = self.espdev.devices.get(m.group(1)) if m and self.espdev else None
        if not dev or dev.get("driver") != "nuki":
            return None, None
        return dev, next((a for a in dev["actions"] if a["id"] == m.group(2) and not a["args"]), None)

    def is_danger(self, s: dict) -> bool:
        """Een signaal dat een deur kan openen. Een actie die we niet kunnen nalezen (het slot is weg) telt ook, want dat is de veilige kant."""
        if not ACT_RE.match(s.get("ref") or ""):
            return False
        _, a = self._action(s["ref"])
        return a is None or a["danger"]

    def _clean(self, sid: str, raw) -> dict:
        if not isinstance(sid, str) or not lx.ID_RE.match(sid) or sid in lx.RESERVED:
            raise ModelError("naam van het signaal: kleine letters, cijfers en _, tot 40 tekens")
        if not isinstance(raw, dict):
            raise ModelError("signaal verwacht")
        d = raw.get("dir")
        if d not in ("out", "in"):
            raise ModelError("richting: out (naar Loxone) of in (van Loxone)")
        ref = raw.get("ref")
        if not isinstance(ref, str):
            raise ModelError("kies een bron")
        if d == "in":
            if not (REF_RE.match(ref) or ACT_RE.match(ref)):
                raise ModelError("van Loxone kan alleen een punt van een apparaat (e:<apparaat>:<punt>) of een actie van een slot worden gezet")
        elif not (SOURCE_RE.match(ref) or ONLINE_RE.match(ref)):
            raise ModelError("onbekende bron")
        kind = raw.get("kind") if raw.get("kind") in KINDS else "float"
        dec, lo, hi = raw.get("decimals"), raw.get("min"), raw.get("max")
        dev, p = self._point(ref)
        if p:  # het punt bepaalt wat het is
            kind = "bool" if p["kind"] == "bool" else "float" if p["kind"] == "float" else "int"
            dec, lo, hi = p["decimals"], p["min"], p["max"]
        elif ONLINE_RE.match(ref) or ACT_RE.match(ref):
            kind, dec, lo, hi = "bool", 0, 0, 1
        dec = dec if isinstance(dec, int) and not isinstance(dec, bool) and 0 <= dec <= 6 else (1 if kind == "float" else 0)
        num = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) else None  # noqa: E731
        try:
            deadband = max(0.0, float(raw.get("deadband") or 0))
            heartbeat = _int(raw.get("heartbeat", HEARTBEAT), 5, 3600, "herhaling")
        except (TypeError, ValueError):
            raise ModelError("dode band en herhaling zijn getallen") from None
        return {"dir": d, "ref": ref, "label": str(raw.get("label") or "")[:120], "kind": kind, "decimals": dec, "min": num(lo), "max": num(hi),
                "deadband": deadband, "heartbeat": heartbeat, "skip_first": raw.get("skip_first") is True, "enabled": raw.get("enabled") is not False,
                "ack": raw.get("ack") is True}  # ack: de gebruiker bevestigde dat Loxone hiermee een deur mag openen

    def add_signal(self, sid: str, raw: dict) -> None:
        if sid in self.signals:
            raise ModelError(f"{sid} bestaat al")
        if len(self.signals) >= MAX_SIGNALS:
            raise ModelError(f"hoogstens {MAX_SIGNALS} signalen")
        s = self._clean(sid, raw)
        self._check_in(s)
        self.signals[sid] = s
        self._save()
        self._poke()

    def update_signal(self, sid: str, b: dict) -> None:
        old = self.signals.get(sid)
        if not old:
            raise ModelError("onbekend signaal")
        s = self._clean(sid, {**old, **b})
        self._check_in(s)
        self.signals[sid] = s
        self.sent.pop(sid, None)
        self._save()
        self._poke()

    def remove_signal(self, sid: str) -> bool:
        if sid not in self.signals:
            return False
        del self.signals[sid]
        self.sent.pop(sid, None)
        self.got.pop(sid, None)
        self.queue.pop(sid, None)
        self._save()
        self._poke()
        return True

    def _check_in(self, s: dict) -> None:
        """Wat Loxone mag zetten: een bestaand, instelbaar punt dat niet gevaarlijk is."""
        if s["dir"] != "in" or not self.espdev:
            return
        if ACT_RE.match(s["ref"]):
            dev, a = self._action(s["ref"])
            if not a:
                raise ModelError("onbekende actie (alleen acties zonder argumenten van een Nuki-slot)")
            if a["danger"]:
                if not s["ack"]:
                    raise ModelError(f"{a['label']} opent de deur: bevestig dat Loxone dit mag starten")
                if self.transport != "http":
                    raise ModelError(f"{a['label']} opent de deur en wordt alleen over HTTP aangenomen: zet de verbinding van Loxone naar de add-on eerst op HTTP")
            return
        dev, p = self._point(s["ref"])
        if not p:
            raise ModelError("onbekend punt")
        if not p["writable"]:
            raise ModelError(f"{p['label']} is niet instelbaar")
        if p["danger"]:
            raise ModelError(f"{p['label']} is gevaarlijk (bv. een deur) en wordt nooit door Loxone gezet")

    def candidates(self, did: str) -> list[dict]:
        """Wat van een apparaat een signaal kan worden, voor de keuzelijst: de punten die op zijn blok staan, met richting."""
        dev = self.espdev.devices.get(did) if self.espdev else None
        if not dev:
            raise ModelError("onbekend apparaat")
        shown = self.espdev.shown(dev)
        out = [{"pid": p["id"], "label": p["label"], "kind": p["kind"], "out": True, "in": p["writable"] and not p["danger"], "on": p["id"] in shown}
               for p in dev["points"] if p["kind"] != "text" and not p.get("gone")]
        return out

    def add_device(self, did: str, prefix: str, points: list[dict] | None = None, channel: str | None = None) -> list[str]:
        """Signalen voor de punten van een apparaat: `<voorvoegsel>_<punt>` (naar Loxone) en `<voorvoegsel>_<punt>_set` (van Loxone), plus `<voorvoegsel>_ok`.
        Zonder keuze: de punten die op het blok staan; instelbare punten krijgen ook een ingaand signaal."""
        if channel not in (None, "", MAIN) and channel not in self.channels:
            raise ModelError("onbekend kanaal")
        prefix = str(prefix or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{1,24}", prefix):
            raise ModelError("voorvoegsel: kleine letters, cijfers en _, tot 24 tekens")
        cands = {c["pid"]: c for c in self.candidates(did)}
        dev = self.espdev.devices[did]
        if points is None:
            points = [{"pid": pid, "out": True, "in": c["in"]} for pid, c in cands.items() if c["on"]]
        made, new = [], {}
        for pt in points:
            c = cands.get(pt.get("pid"))
            if not c:
                raise ModelError(f"onbekend punt {pt.get('pid')}")
            p = next(x for x in dev["points"] if x["id"] == c["pid"])
            label = f"{dev['name']}: {p['label']}"
            if p["kind"] == "enum":
                label += " (" + ", ".join(f"{o['v']}={o['l']}" for o in p["options"][:12]) + ")"
            if pt.get("out"):
                new[f"{prefix}_{c['pid']}"] = {"dir": "out", "ref": f"e:{did}:{c['pid']}", "label": label}
            if pt.get("in"):
                if not c["in"]:
                    raise ModelError(f"{p['label']} kan Loxone niet zetten")
                new[f"{prefix}_{c['pid']}_set"] = {"dir": "in", "ref": f"e:{did}:{c['pid']}", "label": label}
        new[f"{prefix}_ok"] = {"dir": "out", "ref": f"online:{did}", "label": f"{dev['name']}: bereikbaar (1) of niet (0)"}
        cleaned = {}
        for sid, raw in new.items():
            if sid in self.signals:
                continue  # eerder toegevoegd: niet overschrijven
            cleaned[sid] = self._clean(sid, raw)
            made.append(sid)
        if len(self.signals) + len(cleaned) > MAX_SIGNALS:
            raise ModelError(f"hoogstens {MAX_SIGNALS} signalen")
        self.signals.update(cleaned)
        if channel and channel != MAIN and cleaned:
            self.assign[did] = channel
        self._save()
        self._poke()
        return made

    # ------------------------------------------------------------ naar Loxone

    def _read(self, ref: str):
        """De waarde van een bron als getal, of None (onbekend)."""
        m = ONLINE_RE.match(ref)
        if m:
            return 1 if self.espdev and self.espdev.is_online(m.group(1)) else 0
        v = self.engine.ref_value(ref)
        if v is None or isinstance(v, str):
            return None
        return int(v) if isinstance(v, bool) else v

    def request_snapshot(self) -> None:
        """Alles opnieuw sturen, nu (Loxone vroeg erom, of de gebruiker)."""
        self.sent.clear()
        self._t0 = -1e9

    def tick(self) -> None:
        """Een ronde: wat nu moet worden verstuurd, in de rij zetten."""
        if not self.miniserver:
            return
        now = self.clock()
        if now - self._t0 < self.start_delay:
            return
        for sid, s in self.signals.items():
            if s["dir"] != "out" or not s["enabled"]:
                continue
            try:
                v = self._read(s["ref"])
            except Exception:  # noqa: BLE001 - één kapotte bron mag de rest niet tegenhouden
                log.exception("bron %s", s["ref"])
                continue
            if v is None:
                continue
            txt = lx.fmt(v, s["decimals"])
            st = self.sent.get(sid)
            due = st is None or now - st["t"] >= s["heartbeat"]
            if not due and txt != st["txt"] and now - st["t"] >= MIN_GAP:
                due = s["deadband"] <= 0 or abs(float(v) - st["v"]) >= s["deadband"]
            if due:
                self.queue[sid] = lx.message(sid, v, s["decimals"])
                self.sent[sid] = {"txt": txt, "v": float(v), "t": now, "wall": time.time()}

    async def _pump(self) -> None:
        while True:
            if self.queue and self._udp and self.miniserver:
                sid, data = self.queue.popitem(last=False)
                try:
                    sg = self.signals.get(sid)
                    self._udp.sendto(data, (self.miniserver, self.channel_port(self.channel_of(sg)) if sg else self.out_port))
                    self.stats["sent"] += 1
                    self._note("uit", data.decode("ascii", "replace"), str(self.channel_port(self.channel_of(sg)) if sg else self.out_port), "")
                except OSError as e:
                    self.error = f"versturen mislukt: {e}"
            await asyncio.sleep(self.gap)

    async def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("loxone")
            await asyncio.sleep(self.tick_every)

    # ------------------------------------------------------------ van Loxone

    def _allowed(self, ip) -> bool:
        if not self.miniserver:
            if not self._warned:
                self._warned = True
                log.warning("bericht van %s genegeerd: het adres van de Miniserver is niet ingesteld", ip)
            return False
        return ip == self.miniserver

    async def apply(self, pairs: list[tuple[str, float]], via: str = "udp") -> list[tuple[str, str]]:
        """Waarden van Loxone toepassen (`via`: udp of http). Per signaal een uitslag: 'ok', 'zelfde', 'onthouden' of de reden van de weigering."""
        res = []
        for sid, value in pairs:
            r = await self._apply_one(sid, value, via)
            self._note("in", f"kk.{sid}={value:g}", via.upper(), r)
            res.append((sid, r))
        return res

    def _note(self, d: str, text: str, via: str, result: str) -> None:
        self.traffic.append({"t": time.time(), "d": d, "txt": text, "via": via, "result": result})

    async def _apply_action(self, sid: str, s: dict, value: float, via: str, out: dict) -> str:
        """Loxone start een actie van een slot. Strenger dan een punt, want het kan een deur zijn (dezelfde regels als de ingang op het blok, DESIGN §37):
        - alleen een 1 start iets; een 0 niet (en een gevaarlijke actie start alleen bij de **overgang** van 0 naar 1: een herstarte Miniserver die zijn 1 nog eens stuurt
          opent niets, en de eerste waarde na een start van de add-on wordt alleen onthouden);
        - een gevaarlijke actie komt alleen over HTTP (TCP is niet zomaar te vervalsen; een UDP-pakket wel) en alleen als de gebruiker dat bevestigde (`ack`);
        - hoogstens eens per `ACTION_GAP` per signaal, en nooit wachten: is het slot niet bereikbaar, dan vervalt de opdracht, nooit opnieuw proberen;
        - elke gestarte actie staat in het logboek."""
        prev, v = self.act_mem.get(sid), value >= 0.5
        self.act_mem[sid] = v
        try:
            dev, a = self._action(s["ref"])
            if not a:
                raise ModelError("onbekende actie")
            if not v:
                out["result"] = "0: niets gedaan"
                return "ok"
            if a["danger"]:
                if not s["ack"]:
                    raise ModelError("niet bevestigd: deze actie opent de deur")
                if via != "http":
                    raise ModelError("deze actie opent de deur en wordt alleen over HTTP aangenomen")
                if prev is not False:
                    raise ModelError("geen overgang van 0 naar 1: niet uitgevoerd")
            now = self.clock()
            if now - self.act_last.get(sid, -1e9) < ACTION_GAP:
                raise ModelError(f"te kort na de vorige keer (hoogstens eens per {ACTION_GAP:g} s)")
            did = ACT_RE.match(s["ref"]).group(1)
            if not self.espdev.is_online(did):
                raise ModelError(f"{dev['name']} is niet bereikbaar: de opdracht vervalt")
            self.act_last[sid] = now
            log.warning("Loxone start %s op %s (signaal %s, via %s)", a["label"], dev["name"], sid, via)
            await self.espdev.do(did, a["id"], None, confirm=True)
        except ModelError as e:
            self.stats["refused"] += 1
            out["result"] = str(e)
            log.warning("%s=%s geweigerd: %s", sid, value, e)
            return str(e)
        out["result"] = "gestart"
        return "ok"

    async def _apply_one(self, sid: str, value: float, via: str = "udp") -> str:
        self.stats["received"] += 1
        if sid == "sync":
            self.request_snapshot()
            return "ok"
        s = self.signals.get(sid)
        if not s or s["dir"] != "in" or not s["enabled"]:
            self.stats["unknown"] += 1
            return "onbekend signaal"
        first = sid not in self.seen
        self.seen.add(sid)
        out = self.got[sid] = {"v": value, "wall": time.time(), "result": ""}
        if first and s["skip_first"]:
            out["result"] = "onthouden"
            return "onthouden"
        if ACT_RE.match(s["ref"]):
            return await self._apply_action(sid, s, value, via, out)
        dev, p = self._point(s["ref"])
        try:
            if not p or not self.espdev:
                raise ModelError("onbekend punt")
            if not p["writable"] or p["danger"]:
                raise ModelError("dit punt wordt niet door Loxone gezet")
            v = value >= 0.5 if p["kind"] == "bool" else _snap(p, value)
            did = REF_RE.match(s["ref"]).group(1)
            cur = self.espdev.output(s["ref"])
            if cur is not None and (bool(cur) == v if p["kind"] == "bool" else abs(float(cur) - float(v)) < 1e-9):
                out["result"] = "zelfde"
                return "zelfde"
            await self.espdev.set(did, p["id"], v)
        except ModelError as e:
            self.stats["refused"] += 1
            out["result"] = str(e)
            log.info("%s=%s geweigerd: %s", sid, value, e)
            return str(e)
        out["result"] = "ok"
        return "ok"

    def on_datagram(self, data: bytes, addr) -> None:
        if not self._allowed(addr[0]):
            return
        pairs = lx.parse(data)
        if pairs:
            asyncio.get_running_loop().create_task(self._safe(pairs))

    async def _safe(self, pairs) -> None:
        try:
            await self.apply(pairs)
        except Exception:  # noqa: BLE001
            log.exception("bericht van Loxone")

    async def _http(self, request: web.Request) -> web.Response:
        if not self._allowed(request.remote):
            return web.Response(status=403, text="niet toegestaan\n")
        text = "\n".join(f"{k}={v}" for k, v in request.query.items())
        if request.method == "POST":
            text += "\n" + (await request.text())[: lx.MAX_BYTES]
        pairs = lx.parse(text)
        if not pairs:
            return web.Response(status=400, text="geen geldig bericht (kk.<naam>=<getal>)\n")
        res = await self.apply(pairs, "http")
        bad = [f"{sid}: {r}" for sid, r in res if r not in ("ok", "zelfde", "onthouden")]
        return web.Response(status=400 if bad else 200, text=("\n".join(bad) if bad else "ok") + "\n")

    # ------------------------------------------------------------ starten en stoppen

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self._udp, _ = await loop.create_datagram_endpoint(lambda: _Udp(self.on_datagram), local_addr=("0.0.0.0", self.port))
            self.udp_port = self._udp.get_extra_info("sockname")[1]
        except OSError as e:
            self.error = f"UDP-poort {self.port}: {e}"
            log.error("%s", self.error)
        app = web.Application()
        app.router.add_route("*", "/loxone", self._http)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            site = web.TCPSite(self._runner, "0.0.0.0", self.port)
            await site.start()
            self.http_port = site._server.sockets[0].getsockname()[1]
        except OSError as e:
            self.error = f"{self.error} TCP-poort {self.port}: {e}".strip()
            log.error("HTTP-poort %s: %s", self.port, e)
        self._tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._loop())]
        log.info("Loxone: luistert op %s (UDP %s, HTTP %s), Miniserver %s", self.port, self.udp_port, self.http_port, self.miniserver or "niet ingesteld")

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._udp:
            self._udp.close()
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------ de UI en het sjabloon

    def me(self) -> str:
        """Het IP van de add-on zoals de Miniserver het ziet: ingesteld, anders afgeleid van de weg naar de Miniserver."""
        return self.addon_host or local_ip(self.miniserver)

    def view(self) -> dict:
        now = time.time()
        sigs = []
        order = sorted(self.signals.items(), key=lambda kv: (self.group_name(self.group_of(kv[1])).lower(), kv[0]))
        for sid, s in order:
            g = self.group_of(s)
            row = {"id": sid, **s, "key": lx.key(sid), "group": g, "group_name": self.group_name(g), "channel": self.channel_of(s), "action": bool(ACT_RE.match(s["ref"])), "danger": self.is_danger(s)}
            if s["dir"] == "out":
                st = self.sent.get(sid)
                try:
                    v = self._read(s["ref"])
                except Exception:  # noqa: BLE001
                    v = None
                row.update(value=v, last=st["wall"] if st else None)
            else:
                g = self.got.get(sid)
                try:
                    v = self.espdev.output(s["ref"]) if self.espdev else None
                except Exception:  # noqa: BLE001
                    v = None
                row.update(value=v, last=g["wall"] if g else None, got=g["v"] if g else None, result=g["result"] if g else "")
            sigs.append(row)
        return {"miniserver": self.miniserver, "out_port": self.out_port, "addon_host": self.addon_host, "me": self.me(), "transport": self.transport,
                "port": self.port, "udp": self.udp_port, "http": self.http_port, "listen_error": self.error, "stats": dict(self.stats), "queue": len(self.queue),
                "signals": sigs, "now": now, "traffic": list(self.traffic)[::-1],
                "channels": [{**c, "n_out": sum(1 for s in sigs if s["channel"] == c["id"] and s["dir"] == "out" and s["enabled"]),
                              "n_in": sum(1 for s in sigs if s["channel"] == c["id"] and s["dir"] == "in" and s["enabled"]),
                              "devices": sorted({s["group_name"] for s in sigs if s["channel"] == c["id"]})} for c in self.channel_list()]}

    @staticmethod
    def group_of(s: dict) -> str:
        """Bij welk apparaat een signaal hoort: het id van het apparaat, of `-` voor een bron zonder apparaat (timer, module)."""
        m = REF_RE.match(s.get("ref") or "") or ONLINE_RE.match(s.get("ref") or "") or ACT_RE.match(s.get("ref") or "")
        return m.group(1) if m else NO_DEVICE

    def group_name(self, g: str) -> str:
        if g == NO_DEVICE:
            return "Overig"
        dev = self.espdev.devices.get(g) if self.espdev else None
        return dev["name"] if dev else g

    def _sorted(self, devices=None, channel: str = MAIN) -> list[dict]:
        """De ingeschakelde signalen van een kanaal, per apparaat en dan op naam; met `devices` (een lijst apparaat-id's, `-` voor overig) alleen die van die apparaten."""
        want = set(devices) if devices is not None else None
        rows = [{"id": sid, **s} for sid, s in self.signals.items()
                if s["enabled"] and self.channel_of(s) == channel and (want is None or self.group_of(s) in want)]
        return sorted(rows, key=lambda r: (self.group_name(self.group_of(r)).lower(), r["id"]))

    def _title(self, base: str, devices, channel: str = MAIN) -> str:
        """Een bestand van een ander kanaal of van een paar apparaten krijgt dat in de titel, zodat twee imports in Config uit elkaar te houden zijn."""
        if channel != MAIN:
            base = f"{base} - {self.channel_name(channel)}"
        if devices is None:
            return base
        names = sorted({self.group_name(g) for g in devices})
        return f"{base} - " + (", ".join(names) if 0 < len(names) <= 3 else f"{len(names)} apparaten")

    def _known(self, channel: str) -> None:
        if channel != MAIN and channel not in self.channels:
            raise ModelError("onbekend kanaal")

    def viu(self, devices=None, channel: str = MAIN) -> str:
        if not self.me():
            raise ModelError("stel eerst het adres van de Miniserver of van de add-on in")
        self._known(channel)
        return lx.viu_xml(self._title("Huisconfig naar Loxone", devices, channel), self.me(), self.channel_port(channel),
                          [s for s in self._sorted(devices, channel) if s["dir"] == "out"])

    def vo(self, devices=None, channel: str = MAIN) -> str:
        if not self.me():
            raise ModelError("stel eerst het adres van de Miniserver of van de add-on in")
        self._known(channel)
        return lx.vo_xml(self._title("Huisconfig van Loxone", devices, channel), self.me(), self._listen_port(), self.transport,
                         [s for s in self._sorted(devices, channel) if s["dir"] == "in"])

    def _listen_port(self) -> int:
        """De poort waarop de add-on luistert, voor het soort verbinding dat Loxone gebruikt."""
        return (self.http_port if self.transport == "http" else self.udp_port) or self.port

    def table(self, devices=None, channel: str = MAIN) -> str:
        self._known(channel)
        return lx.table(self.me(), self.channel_port(channel), self.me(), self._listen_port(), self.transport, self._sorted(devices, channel))


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, cb) -> None:
        self.cb = cb

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self.cb(data, addr)
        except Exception:  # noqa: BLE001 - een rare datagram mag het luisteren niet stoppen
            log.exception("Loxone-datagram")
