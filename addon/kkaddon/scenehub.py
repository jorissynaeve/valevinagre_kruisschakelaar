"""Scènes voor HUISDEV-apparaten (protocol 1.1, protocol/huisdev/HUISDEV.md §16) aan de kant van de add-on.

Een scène is een kleine binaire beschrijving van een lichtverloop (protocol/ledscene/LEDSCENE.md, gemaakt door scenes.py) die een ESP zelf
opslaat en afspeelt. Dit bestand is alles wat de add-on daarvoor doet; espdevhub.py roept het op een handvol plaatsen aan (zie ADDON.md).

  bibliotheek   /data/scenes.json: id (s1, s2, ... nooit hergebruikt), naam, definitie (kind static, fade, sunrise, wake, rainbow, raw) en `release`
  per apparaat  dev["scn"] = {"sel": [ids], "slots": {id: slot}, "cap": wat het apparaat in zijn descriptor meldt}; de rij van een scène is een
                bool-ingang `scene:<id>` in dev["inputs"] (een punt-id mag geen `:` hebben, dus dit botst nooit met een punt)
  ingang        0 naar 1 start, 1 naar 0 stopt (alleen bij `release` stop). Een verzender per apparaat; een start vervalt na 60 s zonder verbinding,
                een stop blijft geldig zodra het apparaat terug is
  sync          na elke poll: een slot vullen of wissen als het afwijkt (hoogstens een schrijfactie per ronde en per 10 minuten per slot en hash,
                6 per uur), en de geplande starts van een wekker waarvan de bron een timer is (de ESP start hem dan zelf, ook zonder verbinding)

Alles is absoluut en idempotent: een scène die al speelt opnieuw starten, een slot opnieuw vullen of dezelfde lijst opnieuw sturen doet niets."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import scenes as sc
from .logic import BOOL
from .model import ModelError, fits, source_type, timer_active

log = logging.getLogger("kk.scenes")

KEY = "scene:"  # in dev["inputs"], dev["last"] en de poorten van het blok: scene:<id>
SID_RE = re.compile(r"^s[0-9]{1,6}$")
SELF_RE = re.compile(r"^e:([a-z0-9_]{1,40}):")
MAX_SCENES = 64
MAX_SEL = 16  # scènes per apparaat (een rij per scène op het blok)
MAX_NAME = 60
MAX_BYTES = 1024  # grootste scène die ooit op een apparaat past (HUISDEV §16)
START_TTL = 60.0  # s: een start is te laat als het apparaat zo lang niet bereikbaar was (de scène heeft een `resume`-venster, een wekker die een uur te laat start is erger dan geen)
STOP_TTL = 6 * 3600.0  # s: een stop blijft geldig, maar niet eeuwig
WAIT = 1.0  # s: zolang het apparaat offline is, kijken we zo vaak of het terug is
RETRY_GAP = 2.0  # s tussen twee pogingen
MAX_TRIES = 5  # pogingen per opdracht bij een netwerkfout (tellen alleen terwijl het apparaat online is)
MAX_QUEUE = 20
STORE_GAP = 600.0  # s: dezelfde (slot, hash) hoogstens zo vaak schrijven (een apparaat dat de hash niet aanneemt veroorzaakt zo geen lus)
STORE_HOUR = 6  # schrijfacties (store, wissen) per uur per apparaat
SCHED_HOUR = 6  # idem voor `schedule`
SCHED_LOW = 3  # een apparaat met minder geplande regels dan dit krijgt weer een volle lijst
SCHED_GRACE = 90  # s: een regel die zo dichtbij is (of net voorbij, het apparaat loopt misschien een paar seconden achter) doet niet mee in de vergelijking en gaat niet verloren bij een herschrijven
SCHED_DAYS = 13  # dagen vooruit (het apparaat weigert alles na nu + 14 dagen)
RELEASES = ("stop", "finish")
VALID_TIME = 1_700_000_000  # een klok van het apparaat is pas geldig boven dit getal (HUISDEV §16)


class _Fail(ModelError):
    """Een opdracht aan het apparaat die niet lukte. `retry`: een netwerkfout, zinvol om opnieuw te proberen."""

    def __init__(self, msg: str, retry: bool = False) -> None:
        super().__init__(msg)
        self.retry = retry


# ------------------------------------------------------------ wat een apparaat meldt en wat we bewaren (puur)

def _int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def clean_cap(raw) -> dict | None:
    """Het `scenes`-object uit de descriptor, gecontroleerd. Ongeldig is None (het apparaat heeft dan geen scènes), nooit een fout."""
    if not isinstance(raw, dict) or not isinstance(raw.get("formats"), list):
        return None
    slots, mb, sched = raw.get("slots"), raw.get("max_bytes"), raw.get("sched")
    if not (_int(slots) and _int(mb) and _int(sched)) or not 1 <= slots <= 8 or not 1 <= mb <= MAX_BYTES or not 0 <= sched <= 8 or slots * mb > 6144:
        return None
    return {"formats": [f[:30] for f in raw["formats"] if isinstance(f, str)][:8], "slots": slots, "max_bytes": mb, "sched": sched}


def cap_field(raw) -> dict:
    """Voor `clean_descriptor`: {"scenes": ...} of niets."""
    cap = clean_cap(raw)
    return {"scenes": cap} if cap else {}


def clean_scn(raw) -> dict:
    """dev["scn"] uit espdev.json: een ontbrekend of beschadigd deel is gewoon leeg."""
    raw = raw if isinstance(raw, dict) else {}
    sel = []
    for x in raw.get("sel") if isinstance(raw.get("sel"), list) else []:
        if isinstance(x, str) and SID_RE.match(x) and x not in sel and len(sel) < MAX_SEL:
            sel.append(x)
    slots = {}
    used = set()
    for sid, n in (raw.get("slots") if isinstance(raw.get("slots"), dict) else {}).items():
        if sid in sel and _int(n) and 1 <= n <= 8 and n not in used:
            slots[sid] = n
            used.add(n)
    return {"sel": sel, "slots": slots, "cap": clean_cap(raw.get("cap"))}


def block_ports(a: dict, first_row: int, ports: list) -> int:
    """Voor `schema._espdev_blocks`: een rij met een bool-ingang per geselecteerde scène (zoals een Nuki-actie). Geeft het aantal rijen."""
    scn = a.get("scn") or {}
    if not scn.get("cap"):
        return 0  # een apparaat zonder scènes: de bedrading slaapt
    sel = scn.get("sel") or []
    for i, s in enumerate(sel):
        ports.append({"name": KEY + s["id"], "type": "bool", "label": s["name"], "ref": a["inputs"].get(KEY + s["id"]), "row": first_row + i, "action": True, "scene": True, "danger": False})
    return len(sel)


def needs_write(have: list[dict], want: list[dict], low: int = SCHED_LOW, after: float | None = None) -> bool:
    """Moet de geplande lijst van het apparaat (`have`) vervangen worden door `want`? Een verlopen regel is geen reden (het apparaat haalt hem zelf weg):
    alleen een lege lijst die gevuld hoort te zijn, een lijst die leeg moet, minder dan `low` regels over terwijl er meer te plannen is, of een verschil
    in het deel dat beide lijsten dekken (een timer die gewijzigd is). `after`: regels die niet later zijn dan dit tellen in beide lijsten niet mee."""
    h = [(e["at"], e["slot"]) for e in have if after is None or e["at"] > after]
    w = [(e["at"], e["slot"]) for e in want if after is None or e["at"] > after]
    if h == w:
        return False
    if not w:
        return True
    if not h or len(h) < low:
        return True
    end = min(h[-1][0], w[-1][0])
    return [e for e in h if e[0] <= end] != [e for e in w if e[0] <= end]


@lru_cache(maxsize=64)
def _edges(key: str, now_min: int, count: int, tz) -> tuple:
    days = json.loads(key)
    timer = {"days": days}

    def active(e: int) -> bool:
        t = datetime.fromtimestamp(e, tz)
        return timer_active(timer, t.weekday(), t.hour * 60 + t.minute)

    start = (now_min + 1) * 60
    prev = active(start - 60)
    out = []
    for e in range(start, start + SCHED_DAYS * 86400, 60):
        cur = active(e)
        if cur and not prev:
            out.append(e)
            if len(out) >= count:
                break
        prev = cur
    return tuple(out)


def rising_edges(timer: dict, now: float, count: int = 8, tz=None) -> list[int]:
    """De eerstvolgende `count` opkomende flanken (0 naar 1) van een timer als epochs, tot `SCHED_DAYS` dagen vooruit. Gesimuleerd per minuut over de
    echte tijdlijn (epoch, niet de wandklok), dus een zomertijdgrens klopt vanzelf: een interval dat aansluit op het vorige (einde 00:00 en begin 00:00)
    is geen flank, en een flank is wat de engine ook als 0 naar 1 ziet. Een flank in de lopende minuut telt niet meer mee (die hebben we live gezien)."""
    return list(_edges(json.dumps(timer.get("days") or {}, sort_keys=True), int(now) // 60, count, tz))


# ------------------------------------------------------------ de bibliotheek

class Library:
    """/data/scenes.json. Ids worden nooit hergebruikt: `next` telt alleen op."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.next = 1
        self.items: dict[str, dict] = {}
        self.bytes: dict[str, bytes] = {}
        self.hashes: dict[str, str] = {}
        self._load()

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
        items = raw.get("scenes") if isinstance(raw.get("scenes"), dict) else {}
        top = 1
        for sid, v in items.items():
            if not isinstance(sid, str) or not SID_RE.match(sid) or not isinstance(v, dict) or not isinstance(v.get("def"), dict):
                continue
            top = max(top, int(sid[1:]) + 1)
            name = v["name"].strip()[:MAX_NAME] if isinstance(v.get("name"), str) and v["name"].strip() else sid
            self.items[sid] = {"name": name, "def": v["def"], "release": v["release"] if v.get("release") in RELEASES else "stop"}
            try:
                self._index(sid)
            except ModelError as e:  # een definitie die niet meer compileert (een oudere versie): de scène blijft, maar speelt niet
                log.warning("scène %s (%s) compileert niet: %s", sid, name, e)
        n = raw.get("next")
        self.next = max(top, n if _int(n) else 1)

    def _index(self, sid: str) -> None:
        data = self.compile(self.items[sid]["def"])
        self.bytes[sid] = data
        self.hashes[sid] = sc.hash_hex(data)

    @staticmethod
    def compile(defn) -> bytes:
        try:
            data = sc.compile_def(defn)
        except sc.SceneError as e:
            raise ModelError(f"scène: {e}") from None
        if len(data) > MAX_BYTES:
            raise ModelError(f"scène: {len(data)} bytes is te groot voor een apparaat (hoogstens {MAX_BYTES})")
        return data

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"next": self.next, "scenes": self.items}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _clean(b, old: dict | None) -> dict:
        if not isinstance(b, dict):
            raise ModelError("scène: object verwacht")
        out = dict(old) if old else {"name": "", "def": None, "release": "stop"}
        if "name" in b or not old:
            name = b.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ModelError("geef de scène een naam")
            out["name"] = name.strip()[:MAX_NAME]
        if "def" in b or not old:
            try:
                out["def"] = json.loads(json.dumps(b.get("def")))  # een eigen, gewone kopie
            except (TypeError, ValueError):
                raise ModelError("scène: de definitie is geen geldige JSON") from None
        if "release" in b:
            if b["release"] not in RELEASES:
                raise ModelError("release: stop of finish")
            out["release"] = b["release"]
        return out

    def add(self, b) -> str:
        if len(self.items) >= MAX_SCENES:
            raise ModelError(f"maximaal {MAX_SCENES} scènes")
        item = self._clean(b, None)
        data = self.compile(item["def"])
        sid = f"s{self.next}"
        self.next += 1
        self.items[sid] = item
        self.bytes[sid], self.hashes[sid] = data, sc.hash_hex(data)
        self.save()
        return sid

    def update(self, sid: str, b) -> None:
        if sid not in self.items:
            raise ModelError("onbekende scène")
        item = self._clean(b, self.items[sid])
        data = self.compile(item["def"])
        self.items[sid] = item
        self.bytes[sid], self.hashes[sid] = data, sc.hash_hex(data)
        self.save()

    def remove(self, sid: str) -> bool:
        if sid not in self.items:
            return False
        del self.items[sid]
        self.bytes.pop(sid, None)
        self.hashes.pop(sid, None)
        self.save()  # `next` blijft staan: het id wordt nooit opnieuw uitgegeven
        return True

    def name(self, sid: str) -> str:
        return self.items[sid]["name"] if sid in self.items else sid

    def view(self) -> list[dict]:
        out = []
        for sid, it in self.items.items():
            data = self.bytes.get(sid)
            bounded = total = None
            if data:
                s = sc.parse(data)
                bounded, total = sc.is_bounded(s), sc.total_ms(s)
            out.append({"id": sid, "name": it["name"], "def": it["def"], "release": it["release"], "hash": self.hashes.get(sid), "bytes": len(data) if data else None,
                        "bounded": bounded, "total_ms": total})
        return out


def preview(defn, n=20, samples=60) -> dict:
    """Voorbeeld voor de pagina: de renderer bestaat maar een keer, in Python (scenes.preview)."""
    try:
        return sc.preview(defn, n, samples)
    except (sc.SceneError, ValueError) as e:
        raise ModelError(f"scène: {e}") from None


# ------------------------------------------------------------ de scènes van de apparaten

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class SceneHub:
    def __init__(self, hub, mono=None, wall=None) -> None:
        self.hub = hub
        self.lib = Library(Path(hub.path).parent / "scenes.json")
        self.mono = mono or time.monotonic  # de tests zetten hier een eigen klok
        self.wall = wall or (lambda: hub.engine._wall())
        self.rt: dict[str, dict] = {}  # did -> wat het apparaat tot nu meldde en wat we ervan onthouden
        self.q: dict[str, deque] = {}  # did -> opdrachten uit ingangen, in de volgorde van de flanken
        self.locks: dict[str, asyncio.Lock] = {}  # een verzender per apparaat
        self._prune()

    # ------------------------------------------------------------ opruimen

    def _prune(self) -> None:
        """Na het laden: een selectie of bedrading van een scène die niet meer bestaat, verdwijnt."""
        dirty = False
        for dev in self.hub.devices.values():
            for sid in [s for s in dev["scn"]["sel"] if s not in self.lib.items]:
                self._unselect(dev, sid)
                dirty = True
            for key in [k for k in dev["inputs"] if k.startswith(KEY) and k[len(KEY):] not in dev["scn"]["sel"]]:
                dev["inputs"].pop(key)
                dirty = True
        if dirty:
            self.hub._save()

    def _unselect(self, dev: dict, sid: str) -> None:
        scn = dev["scn"]
        if sid in scn["sel"]:
            scn["sel"].remove(sid)
        scn["slots"].pop(sid, None)
        dev["inputs"].pop(KEY + sid, None)

    def drop_device(self, did: str) -> None:
        t = self.hub.tasks.pop(f"scn:{did}", None)
        if t:
            t.cancel()
        for x in (self.rt, self.q, self.locks):
            x.pop(did, None)

    # ------------------------------------------------------------ bibliotheek (voor de web-API)

    def view_library(self) -> list[dict]:
        return self.lib.view()

    def add(self, b) -> str:
        sid = self.lib.add(b)
        self.hub._poke()
        return sid

    def update(self, sid: str, b) -> None:
        self.lib.update(sid, b)
        self.hub._poke()
        for did in self.hub.devices:
            self.hub._kick(did)  # de sync ziet het verschil bij de eerstvolgende poll

    def delete(self, sid: str) -> bool:
        """Weg uit de bibliotheek en overal: de selectie, de bedrading, de slottoewijzing en wat er nog in de wacht stond. De sync wist het slot."""
        if not self.lib.remove(sid):
            return False
        hub = self.hub
        for did, dev in hub.devices.items():
            if sid in dev["scn"]["sel"] or KEY + sid in dev["inputs"]:
                self._unselect(dev, sid)
                hub.seen.get(did, {}).pop(KEY + sid, None)
                hub.last.get(did, {}).pop(KEY + sid, None)
            q = self.q.get(did)
            if q:
                for c in [c for c in q if c["sid"] == sid]:
                    q.remove(c)
            rt = self.rt.get(did)
            if rt:
                rt["started"].pop(sid, None)
            hub._kick(did)
        hub._save()
        hub._poke()
        return True

    # ------------------------------------------------------------ wat een apparaat meldt

    @staticmethod
    def _new() -> dict:
        return {"known": False, "have": {}, "run": None, "sched": [], "time": None, "key": None, "started": {}, "tried": {}, "stores": deque(), "sched_tries": deque(), "sched_sent": None, "snotes": {}, "note_err": False}

    def on_desc(self, did: str, desc: dict) -> None:
        """Een nieuwe descriptor: wat het apparaat aan scènes kan. Zonder `scenes` (of een ander soort apparaat) is er geen scènefunctionaliteit."""
        dev = self.hub.devices[did]
        cap = desc.get("scenes") if dev["driver"] == "huisdev" else None
        if cap != dev["scn"]["cap"]:
            dev["scn"]["cap"] = cap
            self.assign(did)

    def on_state(self, did: str, raw) -> bool:
        """`state.scenes`: have, run, sched en time. True als er iets veranderde dat de pagina moet zien: `time` (elke poll) en `run.until` tellen niet."""
        if not isinstance(raw, dict) and did not in self.rt:
            return False  # een apparaat zonder scènes
        rt = self.rt.setdefault(did, self._new())
        if not isinstance(raw, dict):
            was, rt["known"], rt["key"] = rt["known"], False, None
            rt.update(have={}, run=None, sched=[], time=None)
            return was
        have = {}
        for k, v in (raw.get("have") if isinstance(raw.get("have"), dict) else {}).items():
            if isinstance(k, str) and k.isdigit() and 1 <= int(k) <= 8 and isinstance(v, dict) and isinstance(v.get("h"), str):
                have[int(k)] = {"h": v["h"].lower(), "n": v["n"] if _int(v.get("n")) else 0}
        run = None
        r = raw.get("run")
        if isinstance(r, dict) and isinstance(r.get("h"), str):
            until = r.get("until")
            run = {"slot": r["slot"] if _int(r.get("slot")) and r["slot"] >= 0 else 0, "h": r["h"].lower(), "until": until if _int(until) else None}
        sched = sorted(({"slot": e["slot"], "at": e["at"]} for e in (raw.get("sched") if isinstance(raw.get("sched"), list) else [])
                        if isinstance(e, dict) and _int(e.get("slot")) and _int(e.get("at"))), key=lambda e: (e["at"], e["slot"]))
        t = raw.get("time")
        key = (tuple(sorted((n, v["h"], v["n"]) for n, v in have.items())), (run["slot"], run["h"]) if run else None, tuple((e["at"], e["slot"]) for e in sched))
        changed = key != rt["key"] or not rt["known"]
        rt.update(known=True, have=have, run=run, sched=sched, time=t if _int(t) and t > VALID_TIME else None, key=key)
        return changed

    # ------------------------------------------------------------ selectie en slots

    def select(self, did: str, ids) -> None:
        """Welke scènes op dit apparaat staan (een rij en een slot elk). Wat eruit gaat, verliest zijn bedrading en zijn slot (de sync wist het)."""
        hub = self.hub
        dev = hub.devices.get(did)
        if not dev:
            raise ModelError("onbekend apparaat")
        if dev["driver"] != "huisdev":
            raise ModelError("alleen een HUISDEV-apparaat met scènes kan scènes opslaan")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise ModelError("scenes: een lijst met scène-ids")
        if len(set(ids)) != len(ids):
            raise ModelError("scenes: elke scène hoogstens een keer")
        if len(ids) > MAX_SEL:
            raise ModelError(f"maximaal {MAX_SEL} scènes per apparaat")
        if any(x not in self.lib.items for x in ids):
            raise ModelError("onbekende scène")
        if ids and not dev["scn"]["cap"]:
            raise ModelError("dit apparaat meldt geen scènes (HUISDEV 1.1 met `scenes` in de descriptor)")
        scn = dev["scn"]
        for sid in [s for s in scn["sel"] if s not in ids]:
            self._unselect(dev, sid)
            hub.seen.get(did, {}).pop(KEY + sid, None)
            hub.last.get(did, {}).pop(KEY + sid, None)
        scn["sel"] = list(ids)
        self.assign(did)
        hub._save()
        hub._poke()
        hub._kick(did)

    def assign(self, did: str) -> bool:
        """Een slot voor elke geselecteerde scène: stabiel, het laagste vrije nummer, nooit hergebruikt zolang de scène geselecteerd is. Te veel scènes: de rij blijft, met een melding.
        True als de toewijzing veranderde (de aanroeper bewaart dan)."""
        hub = self.hub
        dev = hub.devices[did]
        scn = dev["scn"]
        cap = scn["cap"]
        if not cap:
            return False
        slots = scn["slots"]
        before = dict(slots)
        for sid in [s for s in slots if s not in scn["sel"] or not 1 <= slots[s] <= cap["slots"]]:
            del slots[sid]
        last = hub.last.setdefault(did, {})
        for sid in scn["sel"]:
            if sid in slots:
                if last.get(KEY + sid, "").startswith("geen vrij slot"):
                    last.pop(KEY + sid)
                continue
            free = [n for n in range(1, cap["slots"] + 1) if n not in slots.values()]
            if free:
                slots[sid] = free[0]
                if last.get(KEY + sid, "").startswith("geen vrij slot"):
                    last.pop(KEY + sid)
            else:
                last[KEY + sid] = f"geen vrij slot: het apparaat heeft {cap['slots']} slots en ze zijn bezet; haal een andere scène van dit apparaat weg"
        return slots != before

    def check_input(self, did: str, dev: dict, pid: str, ref: str) -> None:
        """Mag deze bron op de ingang `scene:<id>`? Alleen een geselecteerde scène en alleen een 0/1."""
        sid = pid[len(KEY):]
        if sid not in dev["scn"]["sel"] or sid not in self.lib.items:
            raise ModelError(f"{pid}: deze scène staat niet op dit apparaat")
        name = self.lib.name(sid)
        if not isinstance(ref, str):
            raise ModelError(f"ingang {name}: ongeldige bron")
        m = self.hub.engine.model
        got = source_type(ref, m.modules, m.timers)
        if got is None:
            raise ModelError(f"ingang {name} wijst naar iets dat niet bestaat")
        if not fits(got, BOOL):
            raise ModelError(f"ingang {name} verwacht een 0 of 1" + (". Zet er een Groter dan of Kleiner dan tussen." if got == "number" else ""))
        r = SELF_RE.match(ref)
        if r and r.group(1) == did:
            raise ModelError("een apparaat stuurt zichzelf niet")

    # ------------------------------------------------------------ de ingangen: flanken

    def _note(self, did: str, key: str, msg: str | None, err: bool = False) -> None:
        if key == "scenes":
            self.rt.setdefault(did, self._new())["note_err"] = err
        last = self.hub.last.setdefault(did, {})
        if msg is None:
            if key in last:
                del last[key]
                self.hub._poke()
        elif last.get(key) != msg:
            last[key] = msg
            self.hub._poke()

    def tick_input(self, did: str, dev: dict, key: str, ref: str, mem: dict) -> None:
        """Een ingang `scene:<id>`: de eerste stand (na een herstart of na onbekend) wordt alleen onthouden; 0 naar 1 start, 1 naar 0 stopt (alleen bij `release` stop)."""
        sid = key[len(KEY):]
        if sid not in dev["scn"]["sel"] or sid not in self.lib.items or not dev["scn"]["cap"]:
            mem.pop(key, None)  # slapend: geen scènes (meer) op dit apparaat
            return
        raw = self.hub.engine.ref_value(ref)
        v = raw if isinstance(raw, bool) else None
        if v is None:
            mem.pop(key, None)
            return
        was = mem.get(key)
        mem[key] = v
        if was is None or was == v:
            return
        if v:
            self._enqueue(did, "start", sid)
        elif self.lib.items[sid]["release"] == "stop":
            self._enqueue(did, "stop", sid)

    def _lock(self, did: str) -> asyncio.Lock:
        return self.locks.setdefault(did, asyncio.Lock())

    def _enqueue(self, did: str, kind: str, sid: str) -> None:
        q = self.q.setdefault(did, deque())
        q.append({"kind": kind, "sid": sid, "t": self.mono(), "tries": 0})
        while len(q) > MAX_QUEUE:
            q.popleft()
        key = f"scn:{did}"
        t = self.hub.tasks.get(key)
        if t is None or t.done():
            self.hub.tasks[key] = asyncio.get_running_loop().create_task(self._worker(did))

    async def _worker(self, did: str) -> None:
        """De enige verzender van de ingangen van een apparaat: in de volgorde van de flanken, de laatste wint."""
        hub = self.hub
        q = self.q.setdefault(did, deque())
        try:
            while q:
                cmd, sid, now = q[0], q[0]["sid"], self.mono()
                name = self.lib.name(sid)
                key = KEY + sid
                if cmd["kind"] == "start" and now - cmd["t"] > START_TTL:
                    q.popleft()
                    self._note(did, key, f"{name}: start vervallen, het apparaat was {int(START_TTL)} s lang niet bereikbaar (te laat om nog te starten)")
                    continue
                if cmd["kind"] == "stop":
                    if now - cmd["t"] > STOP_TTL:
                        q.popleft()
                        self._note(did, key, f"{name}: stop vervallen")
                        continue
                    if cmd.get("waited") and any(c["kind"] == "start" and c["sid"] == sid and now - c["t"] <= START_TTL for c in list(q)[1:]):
                        q.popleft()
                        self._note(did, key, f"{name}: stop vervallen, er was intussen een nieuwere start")
                        continue
                if did not in hub.devices:
                    q.clear()
                    return
                if did not in hub.online:
                    for c in q:
                        c["waited"] = True  # wat moest wachten: een stop met een nieuwere start erachter is dan achterhaald
                    await asyncio.sleep(WAIT)  # een stop wacht tot het apparaat terug is; een start tot hij vervalt
                    continue
                try:
                    async with self._lock(did):
                        msg = await (self._do_start(did, sid) if cmd["kind"] == "start" else self._do_stop(did, sid))
                    q.popleft()
                    self._note(did, key, msg)
                    hub.engine._emit("espdev", what="scene", device=did, scene=sid, op=cmd["kind"], log=False)
                except _Fail as e:
                    cmd["tries"] += 1
                    if not e.retry or cmd["tries"] >= MAX_TRIES:
                        q.popleft()
                        self._note(did, key, f"{name}: {'start' if cmd['kind'] == 'start' else 'stop'} niet verstuurd ({e}); niet opnieuw geprobeerd")
                        log.warning("%s: scène %s (%s) mislukte: %s", hub.devices.get(did, {}).get("name", did), name, cmd["kind"], e)
                    else:
                        await asyncio.sleep(RETRY_GAP)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - een rare fout mag de scènes van dit apparaat niet voorgoed stilleggen
            log.exception("scènes van %s", did)
            q.clear()
        finally:
            if hub.tasks.get(f"scn:{did}") is asyncio.current_task():
                hub.tasks.pop(f"scn:{did}", None)
            hub._poke()

    # ------------------------------------------------------------ opdrachten aan het apparaat (de aanroeper houdt het slot vast)

    async def _post(self, did: str, path: str, body: dict) -> dict:
        from . import espdevhub as eh
        dev = self.hub.devices.get(did)
        if not dev:
            raise _Fail("onbekend apparaat")
        try:
            status, data = await self.hub._call(dev, "POST", path, body)
        except eh.EspError as e:
            raise _Fail(str(e), True) from None
        if status != 200:
            code = data.get("error") if isinstance(data.get("error"), str) else str(status)
            why = eh.ERRORS.get(code, code)
            if data.get("reason") and isinstance(data["reason"], str):
                why = f"{why}: {data['reason']}"
            raise _Fail(why, status == 0 or status >= 500)
        if data.get("result") == "rejected" or data.get("ok") is False:
            raise _Fail(data.get("reason") if isinstance(data.get("reason"), str) and data["reason"] else "het apparaat weigert dit")
        return data

    def _fit(self, dev: dict, data: bytes) -> None:
        cap = dev["scn"]["cap"]
        if not cap:
            raise _Fail("dit apparaat meldt geen scènes")
        if sc.FORMAT not in cap["formats"]:
            raise _Fail(f"het apparaat kent {sc.FORMAT} niet")
        if len(data) > cap["max_bytes"]:
            raise _Fail(f"de scène is {len(data)} bytes, het apparaat neemt hoogstens {cap['max_bytes']}")

    async def _do_start(self, did: str, sid: str) -> str:
        """Start een bibliotheekscène nu. Staat hij al in zijn slot (zelfde hash), dan met `slot`; anders rechtstreeks met de bytes: een live trigger gebruikt altijd
        de actuele bibliotheek en wacht nooit op de sync."""
        dev, rt = self.hub.devices[did], self.rt.setdefault(did, self._new())
        if sid not in self.lib.bytes:
            raise _Fail("de scène bestaat niet (meer) of compileert niet")
        data, h, name = self.lib.bytes[sid], self.lib.hashes[sid], self.lib.name(sid)
        self._fit(dev, data)
        n = dev["scn"]["slots"].get(sid)
        if n and rt["have"].get(n, {}).get("h") == h:
            try:
                await self._post(did, "/huisdev/scene/play", {"slot": n})
                rt["run"] = {"slot": n, "h": h, "until": None}
                rt["started"][sid] = h
                return f"{name}: gestart"
            except _Fail as e:
                if e.retry:
                    raise
                # het slot is intussen anders (een reset): dan rechtstreeks
        await self._post(did, "/huisdev/scene/play", {"data": _b64(data)})
        rt["run"] = {"slot": 0, "h": h, "until": None}
        rt["started"][sid] = h
        return f"{name}: gestart (rechtstreeks)"

    async def _fresh(self, did: str) -> None:
        """De state van het apparaat nu, voor een stop: `run` van een poll van een paar seconden geleden kan achterlopen."""
        from . import espdevhub as eh
        dev = self.hub.devices[did]
        try:
            status, data = await self.hub._call(dev, "GET", "/huisdev/state")
        except eh.EspError as e:
            raise _Fail(str(e), True) from None
        if status != 200:
            raise _Fail(f"het apparaat antwoordt met {status}", status >= 500)
        self.on_state(did, data.get("scenes"))

    async def _do_stop(self, did: str, sid: str) -> str:
        """Stop de scène, maar alleen als hij nog speelt: `stop {slot}` als hij uit een slot speelt, `stop {}` als hij rechtstreeks gestart is. Speelt er iets anders
        (een andere scène, met de hand gestart), dan blijft dat ongemoeid."""
        dev, rt = self.hub.devices[did], self.rt.setdefault(did, self._new())
        name = self.lib.name(sid)
        await self._fresh(did)
        run = rt["run"]
        mine = {h for h in (self.lib.hashes.get(sid), rt["started"].get(sid), rt["have"].get(dev["scn"]["slots"].get(sid), {}).get("h")) if h}
        if not run or run["h"] not in mine:
            return f"{name}: speelt niet meer, niets te stoppen"
        await self._post(did, "/huisdev/scene/stop", {"slot": run["slot"]} if run["slot"] else {})
        rt["run"] = None
        rt["started"].pop(sid, None)
        return f"{name}: gestopt"

    # ------------------------------------------------------------ bedienen vanuit de pagina

    def _ready(self, did: str) -> dict:
        dev = self.hub.devices.get(did)
        if not dev:
            raise ModelError("onbekend apparaat")
        if not dev["scn"]["cap"]:
            raise ModelError("dit apparaat meldt geen scènes")
        if did not in self.hub.online:
            raise ModelError("niet verstuurd: geen verbinding met het apparaat")
        return dev

    async def play(self, did: str, b) -> dict:
        """Speel een bibliotheekscène (`{"id"}`) of een gevuld slot (`{"slot"}`) nu."""
        b = b if isinstance(b, dict) else {}
        dev = self._ready(did)
        async with self._lock(did):
            if "id" in b:
                if not isinstance(b["id"], str) or b["id"] not in self.lib.items:
                    raise ModelError("onbekende scène")
                msg = await self._do_start(did, b["id"])
            else:
                n = b.get("slot")
                if not _int(n) or not 1 <= n <= dev["scn"]["cap"]["slots"]:
                    raise ModelError("slot: een nummer van een slot van dit apparaat")
                have = self.rt.get(did, {}).get("have", {})
                if n not in have:
                    raise ModelError(f"slot {n} is leeg")
                await self._post(did, "/huisdev/scene/play", {"slot": n})
                self.rt[did]["run"] = {"slot": n, "h": have[n]["h"], "until": None}
                msg = f"slot {n}: gestart"
        self.hub._kick(did)
        self.hub.engine._emit("espdev", what="scene", device=did, op="play", log=False)
        return {"ok": True, "message": msg}

    async def stop(self, did: str, b) -> dict:
        """Stop wat speelt (`{}`), alleen een slot (`{"slot"}`) of een bibliotheekscène (`{"id"}`: alleen als hij nog speelt)."""
        b = b if isinstance(b, dict) else {}
        dev = self._ready(did)
        async with self._lock(did):
            if "id" in b:
                if not isinstance(b["id"], str) or b["id"] not in self.lib.items:
                    raise ModelError("onbekende scène")
                msg = await self._do_stop(did, b["id"])
            else:
                body = {}
                if "slot" in b:
                    if not _int(b["slot"]) or not 1 <= b["slot"] <= dev["scn"]["cap"]["slots"]:
                        raise ModelError("slot: een nummer van een slot van dit apparaat")
                    body = {"slot": b["slot"]}
                await self._post(did, "/huisdev/scene/stop", body)
                self.rt.setdefault(did, self._new())["run"] = None
                msg = "gestopt"
        self.hub._kick(did)
        self.hub.engine._emit("espdev", what="scene", device=did, op="stop", log=False)
        return {"ok": True, "message": msg}

    # ------------------------------------------------------------ sync: slots en geplande starts

    async def sync(self, did: str) -> None:
        """Na elke poll: vergelijk wat het apparaat heeft met de bibliotheek. Hoogstens een schrijfactie per ronde; vangt al zijn eigen fouten."""
        try:
            await self._sync(did)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - een fout in de sync mag het volgen van het apparaat niet stilleggen
            log.exception("scènes syncen van %s", did)

    async def _sync(self, did: str) -> None:
        hub = self.hub
        dev, rt = hub.devices.get(did), self.rt.get(did)
        if not dev or dev["driver"] != "huisdev" or not rt or not rt["known"] or did not in hub.online:
            return
        scn = dev["scn"]
        cap = scn["cap"]
        if not cap or not (scn["sel"] or rt["have"] or rt["sched"]):
            return
        if sc.FORMAT not in cap["formats"]:
            self._note(did, "scenes", f"het apparaat kent {sc.FORMAT} niet (het kent: {', '.join(cap['formats']) or 'niets'}): er worden geen scènes opgeslagen", True)
            return
        if self.assign(did):
            hub._save()
        lock = self._lock(did)
        if lock.locked():
            return  # een opdracht van een ingang is onderweg: de volgende ronde
        async with lock:
            if await self._sync_slots(did, dev, rt, cap):
                return
            await self._sync_sched(did, dev, rt, cap)

    def _wanted(self, dev: dict, cap: dict) -> dict[int, tuple[str, str, bytes]]:
        out = {}
        for sid in dev["scn"]["sel"]:
            n = dev["scn"]["slots"].get(sid)
            if n and sid in self.lib.bytes and n <= cap["slots"]:
                out[n] = (sid, self.lib.hashes[sid], self.lib.bytes[sid])
        return out

    async def _sync_slots(self, did: str, dev: dict, rt: dict, cap: dict) -> bool:
        """Een slot vullen of wissen (hoogstens een per ronde). True als er geschreven is (of een poging gedaan)."""
        now = self.mono()
        want = self._wanted(dev, cap)
        todo = []
        for n, (sid, h, data) in sorted(want.items()):
            if rt["have"].get(n, {}).get("h") != h:
                todo.append((n, sid, h, data))
        for n in sorted(rt["have"]):
            if n not in want and n <= cap["slots"]:
                todo.append((n, None, "", b""))  # een slot dat aan geen geselecteerde scène meer hoort
        if not todo:
            if rt["note_err"]:
                self._note(did, "scenes", None)  # een foutmelding van een eerdere ronde is voorbij; "opgeslagen" blijft staan
            return False
        stores = rt["stores"]
        while stores and now - stores[0] > 3600:
            stores.popleft()
        for n, sid, h, data in todo:
            if len(data) > cap["max_bytes"]:
                continue  # de pagina toont "te groot" (view)
            if now - rt["tried"].get((n, h), -1e9) < STORE_GAP:
                continue  # dezelfde (slot, hash) is net geprobeerd: geen lus bij een apparaat dat de hash niet aanneemt
            if len(stores) >= STORE_HOUR:
                self._note(did, "scenes", f"niet opgeslagen: al {STORE_HOUR} schrijfacties dit uur (het flashgeheugen van het apparaat wordt gespaard)", True)
                return False
            rt["tried"][(n, h)] = now
            stores.append(now)
            label = f"slot {n}: {self.lib.name(sid)}" if sid else f"slot {n}"
            try:
                got = await self._post(did, "/huisdev/scene/store", {"slot": n, "data": _b64(data)})
            except _Fail as e:
                self._note(did, "scenes", f"{label}: niet opgeslagen ({e}); opnieuw over {int(STORE_GAP // 60)} minuten", True)
                return True
            if sid and str(got.get("hash", "")).lower() != h:
                self._note(did, "scenes", f"{label}: het apparaat meldt hash {got.get('hash')!r} in plaats van {h}; opnieuw over {int(STORE_GAP // 60)} minuten", True)
                return True
            if sid:
                rt["have"][n] = {"h": h, "n": len(data)}
                self._note(did, "scenes", f"{label}: opgeslagen")
            else:
                rt["have"].pop(n, None)
                self._note(did, "scenes", f"{label}: gewist")
            return True
        return False

    def _snote(self, rt: dict, sid: str, msg: str | None) -> None:
        if msg is None:
            rt["snotes"].pop(sid, None)
        elif rt["snotes"].get(sid) != msg:
            rt["snotes"][sid] = msg
            self.hub._poke()

    def _desired_sched(self, did: str, dev: dict, rt: dict, cap: dict) -> list[dict] | None:
        """De geplande starts die dit apparaat zou moeten hebben, of None als er nog gewacht moet worden (een slot is nog niet gevuld of de klok is niet geldig)."""
        hub = self.hub
        m = hub.engine.model
        now = self.wall()
        tz = getattr(hub.engine, "tz", None)
        want, waiting = [], False
        for sid in dev["scn"]["sel"]:
            ref, n = dev["inputs"].get(KEY + sid), dev["scn"]["slots"].get(sid)
            if not ref or not n or sid not in self.lib.items:
                self._snote(rt, sid, None)
                continue
            t = m.timers.get(ref[2:]) if ref.startswith("t:") else None
            if t is None:
                self._snote(rt, sid, "geen geplande start: de bron is geen timer (alleen een timer is vooruit te berekenen), de gewone flank blijft werken" if not ref.startswith("t:")
                            else "geen geplande start: de timer bestaat niet (meer)")
                continue
            if rt["have"].get(n, {}).get("h") != self.lib.hashes.get(sid):
                waiting = True
                continue
            self._snote(rt, sid, None)
            want += [{"slot": n, "at": at} for at in rising_edges(t, now, cap["sched"], tz)]
        if waiting:
            return None
        return sorted(want, key=lambda e: (e["at"], e["slot"]))[:cap["sched"]]

    async def _sync_sched(self, did: str, dev: dict, rt: dict, cap: dict) -> None:
        if cap["sched"] <= 0 or rt["time"] is None:
            return
        want = self._desired_sched(did, dev, rt, cap)
        near = self.wall() + SCHED_GRACE
        if want is None or not needs_write(rt["sched"], want, after=near):
            return
        # een regel die nu bijna afgaat (of nog niet afging omdat het apparaat iets achterloopt) blijft in de lijst, anders gaat hij verloren
        keep = [e for e in rt["sched"] if near - 2 * SCHED_GRACE < e["at"] <= near and e not in want]
        want = sorted(keep + want, key=lambda e: (e["at"], e["slot"]))[:cap["sched"]]
        now = self.mono()
        key = tuple((e["at"], e["slot"]) for e in want)
        sent = rt["sched_sent"]
        if sent and sent[0] == key and now - sent[1] < STORE_GAP:
            return  # dezelfde lijst is net gestuurd en het apparaat toont hem niet: geen lus
        tries = rt["sched_tries"]
        while tries and now - tries[0] > 3600:
            tries.popleft()
        if len(tries) >= SCHED_HOUR:
            self._note(did, "scenes", f"geplande starts niet gestuurd: al {SCHED_HOUR} keer dit uur", True)
            return
        rt["sched_sent"] = (key, now)
        tries.append(now)
        try:
            await self._post(did, "/huisdev/scene/schedule", {"entries": want})
        except _Fail as e:
            self._note(did, "scenes", f"geplande starts niet gestuurd ({e}); opnieuw over {int(STORE_GAP // 60)} minuten", True)
            return
        rt["sched"] = want
        self._note(did, "scenes", f"{len(want)} geplande {'start' if len(want) == 1 else 'starts'} naar het apparaat gestuurd" if want else "geplande starts gewist")

    # ------------------------------------------------------------ voor de pagina

    def view(self, did: str, dev: dict) -> dict:
        """Wat de pagina van de scènes van dit apparaat weet. Nooit het token."""
        rt = self.rt.get(did) or self._new()
        scn = dev["scn"]
        cap = scn["cap"]
        last = self.hub.last.get(did, {})
        by_slot = {n: sid for sid, n in scn["slots"].items()}
        sel = []
        for sid in scn["sel"]:
            if sid not in self.lib.items:
                continue
            n = scn["slots"].get(sid)
            h = self.lib.hashes.get(sid)
            if h is None:
                state = "broken"
            elif not n:
                state = "noslot"
            elif cap and len(self.lib.bytes[sid]) > cap["max_bytes"]:
                state = "toolarge"
            elif rt["have"].get(n, {}).get("h") == h:
                state = "ok"
            else:
                state = "wait"
            sel.append({"id": sid, "name": self.lib.name(sid), "slot": n, "release": self.lib.items[sid]["release"], "state": state, "src": dev["inputs"].get(KEY + sid), "note": last.get(KEY + sid, ""),
                        "sched_note": rt["snotes"].get(sid, "")})
        run = None
        if rt["run"]:
            r = rt["run"]
            sid = by_slot.get(r["slot"]) if r["slot"] else next((s for s, hh in self.lib.hashes.items() if hh == r["h"]), None)
            run = {"slot": r["slot"], "h": r["h"], "until": r["until"], "id": sid, "name": self.lib.name(sid) if sid else ""}
        sched = [{"slot": e["slot"], "at": e["at"], "id": by_slot.get(e["slot"]), "name": self.lib.name(by_slot[e["slot"]]) if e["slot"] in by_slot else ""} for e in rt["sched"]]
        return {"cap": cap, "known": rt["known"], "sel": sel, "have": {str(n): v for n, v in rt["have"].items()}, "run": run, "sched": sched, "time": rt["time"], "note": last.get("scenes", "")}
