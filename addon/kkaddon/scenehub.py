"""Scènes voor HUISDEV-apparaten (protocol 1.1, protocol/huisdev/HUISDEV.md §16) aan de kant van de add-on.

Een scène is een kleine binaire beschrijving van een lichtverloop (protocol/ledscene/LEDSCENE.md, gemaakt door scenes.py) die een ESP zelf
opslaat en afspeelt. Dit bestand is alles wat de add-on daarvoor doet; espdevhub.py roept het op een handvol plaatsen aan (zie DESIGN.md §42).

  bibliotheek   /data/scenes.json: id (s1, s2, ... nooit hergebruikt), naam, definitie (kind static, fade, sunrise, wake, rainbow, raw) en `release`
  per apparaat  dev["scn"] = {"sel": [ids], "slots": {id: slot}, "cap": wat het apparaat in zijn descriptor meldt}; `scene:<id>` is de sleutel van de melding van een scène
                in dev["last"] (voor 0.45 was het ook een ingang op het apparaatblok: die worden bij het laden een regelblok)
  regels        /data/scenerules.json (scenerules.py): een blok Scènes heeft ingangen (een 0/1 of een tijd) en regels; de eerste regel die klopt bepaalt de scène.
                Verandert die, dan start de nieuwe en stopt de oude alleen als er geen opvolger is en ze `release` stop heeft. Het blok bepaalt ook welke scènes
                op het apparaat staan (`sel`). Een verzender per apparaat; een start vervalt na 60 s zonder verbinding, een stop blijft geldig zodra het apparaat terug is
  sync          na elke poll: een slot vullen of wissen als het afwijkt (hoogstens een schrijfactie per ronde en per 10 minuten per slot en hash,
                6 per uur), en de geplande starts van een regel die alleen uit een timer bestaat (de ESP start hem dan zelf, ook zonder verbinding)
  seek          protocol 1.2: de tijdschuif van de pagina laat de strip het beeld op tijdstip t tonen (`scene/seek`). Een eigen verzender per apparaat die samenvoegt:
                altijd het nieuwste tijdstip, hoogstens 1 per SEEK_GAP; geen invloed op sync, geplande starts, slots of de uurplafonds. Een echte start of stop wint

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
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from . import scenerules as sr
from . import scenes as sc
from .logic import BOOL, TIME
from .model import ModelError, fits, source_type, timer_active

log = logging.getLogger("kk.scenes")

KEY = "scene:"  # in dev["last"]: de melding van een scène; voor 0.45 ook de ingang op het apparaatblok (scene:<id>)
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
SEEK_GAP = 0.125  # s tussen twee seeks naar hetzelfde apparaat (hoogstens 8 per seconde); een nieuwere vervangt wat nog wacht
MAX_SEEK_MS = 86_400_000  # t van `scene/seek` (HUISDEV 1.2)
NO_SEEK = "dit apparaat kan niet door een scène scrollen (firmware ouder dan 1.2)"
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
    cap = {"formats": [f[:30] for f in raw["formats"] if isinstance(f, str)][:8], "slots": slots, "max_bytes": mb, "sched": sched}
    if raw.get("seek") is True:  # protocol 1.2: het apparaat kan het beeld op tijdstip t tonen (`scene/seek`); alleen aanwezig als het waar is
        cap["seek"] = True
    return cap


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
            bounded = total = window = None
            if data:
                s = sc.parse(data)
                bounded, total, window = sc.is_bounded(s), sc.total_ms(s), sc.window_ms(s)
            out.append({"id": sid, "name": it["name"], "def": it["def"], "release": it["release"], "hash": self.hashes.get(sid), "bytes": len(data) if data else None,
                        "bounded": bounded, "total_ms": total, "window_ms": window})
        return out


def preview(defn, n=20, samples=60) -> dict:
    """Voorbeeld voor de pagina: de renderer bestaat maar een keer, in Python (scenes.preview)."""
    try:
        return sc.preview(defn, n, samples)
    except (sc.SceneError, ValueError) as e:
        raise ModelError(f"scène: {e}") from None


def frame_at(defn, t_ms, n=24) -> dict:
    """Het beeld op tijdstip t voor de tijdschuif van de pagina (scenes.frame_at)."""
    try:
        return sc.frame_at(defn, t_ms, n)
    except (sc.SceneError, ValueError) as e:
        raise ModelError(f"scène: {e}") from None


# ------------------------------------------------------------ de scènes van de apparaten

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class SceneHub:
    def __init__(self, hub, mono=None, wall=None) -> None:
        self.hub = hub
        self.lib = Library(Path(hub.path).parent / "scenes.json")
        self.rules = sr.RuleStore(Path(hub.path).parent / "scenerules.json")
        self.mono = mono or time.monotonic  # de tests zetten hier een eigen klok
        self.wall = wall or (lambda: hub.engine._wall())
        self.rt: dict[str, dict] = {}  # did -> wat het apparaat tot nu meldde en wat we ervan onthouden
        self.q: dict[str, deque] = {}  # did -> opdrachten uit de regels, in de volgorde van de wijzigingen
        self.locks: dict[str, asyncio.Lock] = {}  # een verzender per apparaat
        self.rmem: dict[str, dict] = {}  # blok -> {"scene": de scène die de regels de vorige ronde kozen}; ontbreekt = nog geen eerste stand
        self.rnote: dict[str, dict] = {}  # blok -> {"idx": regel die nu klopt, "unsure": bool}, voor de pagina
        self._prune()

    # ------------------------------------------------------------ opruimen en overzetten

    def _prune(self) -> None:
        """Na het laden: een selectie van een scène die niet meer bestaat verdwijnt, ingangen `scene:<id>` van voor 0.45 worden een regelblok en een regel met een
        scène die niet meer bestaat of een apparaat dat weg is, valt weg."""
        dirty = self._migrate()
        for dev in self.hub.devices.values():
            for sid in [s for s in dev["scn"]["sel"] if s not in self.lib.items]:
                self._unselect(dev, sid)
                dirty = True
        changed = False
        for bid, b in self.rules.items.items():
            keep = [r for r in b["rules"] if r["scene"] in self.lib.items]
            if len(keep) != len(b["rules"]):
                b["rules"], changed = keep, True
            if b["device"] and (b["device"] not in self.hub.devices or self.hub.devices[b["device"]]["driver"] != "huisdev"):
                b["device"], changed = "", True
        if changed:
            self.rules.save()
        for did in self.rules.device_owner():
            if did in self.hub.devices:
                dirty = self._apply_sel(did) or dirty
        if dirty:
            self.hub._save()

    def _migrate(self) -> bool:
        """Voor 0.45 had elke scène een ingang `scene:<id>` op het apparaatblok. Die worden een blok Scènes met per ingang een 0/1-ingang en een regel `ingang = 1`:
        hetzelfde gedrag, maar de scènes zitten niet meer in het apparaatblok. Een scène zonder bron had geen ingang en krijgt geen regel (haar slot wordt gewist)."""
        hub, dirty = self.hub, False
        owners = self.rules.device_owner()
        for did, dev in hub.devices.items():
            wired = {k[len(KEY):]: r for k, r in dev["inputs"].items() if k.startswith(KEY)}
            if not wired:
                continue
            ins, rules = [], []
            for sid in dev["scn"]["sel"]:
                if sid in wired and sid in self.lib.items:
                    iid = f"i{len(ins) + 1}"
                    ins.append({"id": iid, "name": self.lib.name(sid), "kind": "bool", "ref": wired[sid]})
                    rules.append({"scene": sid, "when": [{"op": "on", "in": iid}]})
            if ins and did not in owners and len(self.rules.items) < sr.MAX_BLOCKS:
                bid = f"q{self.rules.next}"
                self.rules.next += 1
                self.rules.items[bid] = {"name": f"Scènes {dev['name']}"[:sr.MAX_NAME], "device": did, "inputs": ins, "inext": len(ins) + 1, "rules": rules}
                owners[did] = bid
                self.rules.save()
                self._place_near(f"e:{did}", f"q:{bid}")
                log.info("scènes van %s zijn een blok Scènes geworden (%d regels)", dev["name"], len(rules))
            for k in wired:
                dev["inputs"].pop(KEY + k, None)
            dirty = True
        return dirty

    def _place_near(self, near: str, key: str) -> None:
        """Een nieuw blok op de pagina van een ander blok, ernaast; staat dat nergens, dan blijft het in de lijst."""
        model = getattr(self.hub.engine, "model", None)
        pos = model.schema["blocks"].get(near) if model else None
        if not pos or not pos.get("page"):
            return
        import copy
        s = copy.deepcopy(model.schema)
        x = pos["x"] - 300 if pos["x"] >= 300 else pos["x"] + 520
        s["blocks"][key] = {"page": pos["page"], "x": x, "y": pos["y"]}
        try:
            model.set_schema(s)
        except Exception:  # noqa: BLE001 - een plek is een gemak, geen voorwaarde
            log.exception("plek van %s", key)

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
        self._cancel_seek(did, "apparaat verwijderd")
        t = self.hub.tasks.pop(f"seek:{did}", None)
        if t:
            t.cancel()
        for x in (self.rt, self.q, self.locks):
            x.pop(did, None)
        for bid, b in self.rules.items.items():
            if b["device"] == did:  # het blok blijft, zonder apparaat
                b["device"] = ""
                self.rmem.pop(bid, None)
                self.rules.save()

    # ------------------------------------------------------------ bibliotheek (voor de web-API)

    def view_library(self) -> list[dict]:
        return self.lib.view()

    def frame(self, b) -> dict:
        """`{"def"|"id", "t_ms", "n"?}`: het beeld op dat tijdstip (de pagina tekent het in de voorbeeldstrip)."""
        b = b if isinstance(b, dict) else {}
        if "id" in b and "def" not in b:
            sid = b["id"]
            if not isinstance(sid, str) or sid not in self.lib.bytes:
                raise ModelError("onbekende scène" if not isinstance(sid, str) or sid not in self.lib.items else "de scène compileert niet")
            return frame_at(self.lib.bytes[sid], b.get("t_ms"), b.get("n", 24))
        if not isinstance(b.get("def"), dict):
            raise ModelError("geef def (een definitie) of id (een scène)")
        return frame_at(b["def"], b.get("t_ms"), b.get("n", 24))

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
        touched = []
        for bid, b in self.rules.items.items():  # de regels met deze scène vallen weg
            keep = [r for r in b["rules"] if r["scene"] != sid]
            if len(keep) != len(b["rules"]):
                b["rules"] = keep
                touched.append(b["device"])
        if touched:
            self.rules.save()
        for did in touched:
            if did in hub.devices:
                self._apply_sel(did)
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
        return {"known": False, "have": {}, "run": None, "sched": [], "time": None, "key": None, "started": {}, "tried": {}, "stores": deque(), "sched_tries": deque(), "sched_sent": None, "snotes": {}, "note_err": False,
                "seek_q": None, "seek_cur": None, "seek_at": -1e9, "seek_key": None}

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
            if _int(r.get("seek")) and r["seek"] >= 0:
                run["seek"] = r["seek"]  # het apparaat toont een beeld uit de tijdschuif (1.2); alleen aanwezig als dat zo is
        sched = sorted(({"slot": e["slot"], "at": e["at"]} for e in (raw.get("sched") if isinstance(raw.get("sched"), list) else [])
                        if isinstance(e, dict) and _int(e.get("slot")) and _int(e.get("at"))), key=lambda e: (e["at"], e["slot"]))
        t = raw.get("time")
        key = (tuple(sorted((n, v["h"], v["n"]) for n, v in have.items())), (run["slot"], run["h"], "seek" in run) if run else None, tuple((e["at"], e["slot"]) for e in sched))
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

    def check_ref(self, ref: str, kind: str) -> None:
        """Mag deze bron op een ingang van een blok Scènes? Een 0/1 voor `bool`, een tijd voor `time`."""
        m = self.hub.engine.model
        got = source_type(ref, m.modules, m.timers)
        if got is None:
            raise ModelError("deze ingang wijst naar iets dat niet bestaat")
        if not fits(got, BOOL if kind == "bool" else TIME):
            raise ModelError(f"deze ingang verwacht {'een 0 of 1' if kind == 'bool' else 'een tijd'}" + (". Zet er een Groter dan of Kleiner dan tussen." if kind == "bool" and got == "number" else ""))

    def _apply_sel(self, did: str) -> bool:
        """De scènes van het blok van dit apparaat zijn de scènes die op het apparaat staan (een slot elk). True als er iets veranderde."""
        dev = self.hub.devices.get(did)
        bid = self.rules.device_owner().get(did)
        if not dev or not bid:
            return False
        want = sr.used_scenes(self.rules.items[bid])[:MAX_SEL]
        scn = dev["scn"]
        if scn["sel"] == want:
            return False
        for sid in [x for x in scn["sel"] if x not in want]:
            self._unselect(dev, sid)
            self.hub.last.get(did, {}).pop(KEY + sid, None)
        scn["sel"] = list(want)
        self.assign(did)
        return True

    # ------------------------------------------------------------ de blokken Scènes (voor de web-API)

    def _check_block(self, bid: str | None, b) -> dict:
        devices = {d for d, v in self.hub.devices.items() if v["driver"] == "huisdev"}
        old = self.rules.items.get(bid) if bid else None
        out = sr.clean_block(b, old, self.lib.items, self.check_ref, devices, self.rules.device_owner(), bid)
        used = sr.used_scenes(out)
        if len(used) > MAX_SEL:
            raise ModelError(f"maximaal {MAX_SEL} verschillende scènes per blok")
        return out

    def _block_changed(self, bid: str, old_device: str | None = None) -> None:
        self.rules.save()
        self.rmem.pop(bid, None)  # de regels zijn anders: de eerstvolgende stand wordt weer alleen onthouden
        self.rnote.pop(bid, None)
        hub = self.hub
        for did in {old_device, self.rules.items[bid]["device"] if bid in self.rules.items else None}:
            if did and did in hub.devices:
                if not self.rules.device_owner().get(did):  # het blok is er niet meer: de scènes van dit apparaat horen nergens meer bij
                    self.select(did, [])
                else:
                    self._apply_sel(did)
                hub._kick(did)
        hub._save()
        hub._poke()

    def add_block(self, b) -> str:
        if len(self.rules.items) >= sr.MAX_BLOCKS:
            raise ModelError(f"maximaal {sr.MAX_BLOCKS} blokken Scènes")
        item = self._check_block(None, b)
        bid = f"q{self.rules.next}"
        self.rules.next += 1
        self.rules.items[bid] = item
        self._block_changed(bid)
        return bid

    def update_block(self, bid: str, b) -> None:
        if bid not in self.rules.items:
            raise ModelError("onbekend blok Scènes")
        old_dev = self.rules.items[bid]["device"]
        self.rules.items[bid] = self._check_block(bid, b)
        self._block_changed(bid, old_dev)

    def delete_block(self, bid: str) -> bool:
        if bid not in self.rules.items:
            return False
        dev = self.rules.items.pop(bid)["device"]
        self._block_changed(bid, dev)
        return True

    # ------------------------------------------------------------ de regels: de scène die nu hoort te spelen

    def _read(self, ref):
        return self.hub.engine.ref_value(ref) if isinstance(ref, str) else None

    def _ahead(self, tid: str, minutes: int):
        """Staat de timer over zoveel minuten aan? (voor een wekker die eerder begint dan zijn timer)"""
        eng = self.hub.engine
        t = eng.model.timers.get(tid)
        if t is None:
            return None
        at = eng.local_now() + timedelta(minutes=minutes)
        return timer_active(t, at.weekday(), at.hour * 60 + at.minute)

    def pick(self, bid: str) -> tuple[int | None, bool]:
        """(index van de regel die nu klopt of None, onzeker). Zie scenerules.evaluate."""
        now = self.hub.engine.local_now()
        return sr.evaluate(self.rules.items[bid], self._read, self._ahead, now.hour * 60 + now.minute)

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

    def tick_rules(self) -> None:
        """Elke ronde: de scène die de regels nu kiezen. De eerste stand (na een herstart of na onbekend) wordt alleen onthouden. Een andere scène dan de vorige start
        meteen (en vervangt wat speelde); kiezen de regels niets meer, dan stopt de vorige alleen met `release` stop. Weet een ingang niets en klopt er geen regel,
        dan blijft alles zoals het was."""
        for bid, b in self.rules.items.items():
            did = b["device"]
            dev = self.hub.devices.get(did) if did else None
            if not dev or not dev["scn"]["cap"]:
                self.rmem.pop(bid, None)  # slapend: geen apparaat of geen scènes (meer)
                self.rnote[bid] = {"idx": None, "unsure": False}
                continue
            idx, unsure = self.pick(bid)
            self.rnote[bid] = {"idx": idx, "unsure": unsure}
            sid = b["rules"][idx]["scene"] if idx is not None else None
            if sid is None and unsure:
                self.rmem.pop(bid, None)
                continue
            mem = self.rmem.get(bid)
            self.rmem[bid] = {"scene": sid}
            if mem is None or mem["scene"] == sid:
                continue
            if sid is not None:
                self._enqueue(did, "start", sid)
            elif mem["scene"] in self.lib.items and self.lib.items[mem["scene"]]["release"] == "stop":
                self._enqueue(did, "stop", mem["scene"])

    def _lock(self, did: str) -> asyncio.Lock:
        return self.locks.setdefault(did, asyncio.Lock())

    def _enqueue(self, did: str, kind: str, sid: str) -> None:
        self._cancel_seek(did, "vervangen door een start of stop")  # een echte start of stop wint van de tijdschuif
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
        self._cancel_seek(did, "vervangen door een start")
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
        self._cancel_seek(did, "vervangen door een stop")
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

    # ------------------------------------------------------------ de tijdschuif: scene/seek (protocol 1.2)

    @staticmethod
    def _resolve(req: dict | None, result: dict) -> None:
        if req and not req["fut"].done():
            req["fut"].set_result(result)

    def _cancel_seek(self, did: str, why: str) -> None:
        """Wat nog wacht (of onderweg is) van de tijdschuif vervalt: een start, stop of play wint. De wachtende aanroepers krijgen een gewoon antwoord."""
        rt = self.rt.get(did)
        if not rt:
            return
        for req in (rt["seek_q"], rt["seek_cur"]):
            if req:
                req["dead"] = True
                self._resolve(req, {"ok": True, "message": why, "superseded": True})
        rt["seek_q"] = None
        rt["seek_key"] = None

    def _seek_target(self, dev: dict, rt: dict, b: dict) -> dict:
        """Waar de seek naar kijkt: {"key", "h", "slot", "data"}. `def` en `id` gaan (de eerste keer) met de bytes mee, `slot` met het slotnummer."""
        given = [k for k in ("def", "id", "slot") if k in b]
        if len(given) != 1:
            raise ModelError("geef precies een van def, id of slot")
        if given[0] == "slot":
            n = b["slot"]
            if not _int(n) or not 1 <= n <= dev["scn"]["cap"]["slots"]:
                raise ModelError("slot: een nummer van een slot van dit apparaat")
            have = rt["have"].get(n)
            if not have:
                raise ModelError(f"slot {n} is leeg")
            return {"key": ("slot", n, have["h"]), "h": have["h"], "slot": n, "data": None}
        if given[0] == "id":
            sid = b["id"]
            if not isinstance(sid, str) or sid not in self.lib.items:
                raise ModelError("onbekende scène")
            if sid not in self.lib.bytes:
                raise ModelError("de scène compileert niet")
            data = self.lib.bytes[sid]
        else:
            data = self.lib.compile(b["def"])
        self._fit(dev, data)
        h = sc.hash_hex(data)
        return {"key": ("data", h), "h": h, "slot": None, "data": data}

    async def seek(self, did: str, b) -> dict:
        """De strip toont het beeld van een scène op tijdstip `t_ms` en houdt het vast: `{"def"|"id"|"slot", "t_ms"}`. Seeks naar hetzelfde apparaat worden samengevoegd:
        er gaat hoogstens een verzoek tegelijk en hoogstens 1 per SEEK_GAP, altijd met het nieuwste tijdstip; de aanroepers van wat vervangen werd krijgen `superseded`."""
        b = b if isinstance(b, dict) else {}
        dev = self.hub.devices.get(did)
        if not dev:
            raise ModelError("onbekend apparaat")
        cap = dev["scn"]["cap"]
        if not cap or not cap.get("seek"):
            raise ModelError(NO_SEEK)
        if did not in self.hub.online:
            raise ModelError("niet verstuurd: geen verbinding met het apparaat")
        t = b.get("t_ms")
        if isinstance(t, float) and t.is_integer():
            t = int(t)
        if not _int(t) or not 0 <= t <= MAX_SEEK_MS:
            raise ModelError(f"t_ms: een geheel aantal milliseconden tussen 0 en {MAX_SEEK_MS}")
        rt = self.rt.setdefault(did, self._new())
        target = self._seek_target(dev, rt, b)
        loop = asyncio.get_running_loop()
        req = {"target": target, "t": t, "fut": loop.create_future(), "dead": False}
        self._resolve(rt["seek_q"], {"ok": True, "message": "vervangen door een nieuwere", "superseded": True})
        rt["seek_q"] = req
        key = f"seek:{did}"
        task = self.hub.tasks.get(key)
        if task is None or task.done():
            self.hub.tasks[key] = loop.create_task(self._seek_worker(did))
        return await req["fut"]

    async def _seek_worker(self, did: str) -> None:
        """De enige verzender van seeks van een apparaat: wacht tot de vorige SEEK_GAP geleden is, neemt dan het nieuwste en stuurt het (onder hetzelfde slot als de verzender van
        de ingangen en de sync, dus nooit twee verzoeken tegelijk)."""
        loop = asyncio.get_running_loop()
        rt = self.rt.setdefault(did, self._new())
        try:
            while rt["seek_q"] is not None:
                wait = rt["seek_at"] + SEEK_GAP - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue  # nu het nieuwste
                req, rt["seek_q"] = rt["seek_q"], None
                rt["seek_cur"] = req
                try:
                    async with self._lock(did):
                        if req["dead"]:
                            continue
                        msg = await self._do_seek(did, rt, req)
                    self._resolve(req, {"ok": True, "message": msg})
                except _Fail as e:
                    if not req["fut"].done():
                        req["fut"].set_exception(ModelError(str(e)))
                finally:
                    rt["seek_cur"] = None
                    rt["seek_at"] = loop.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - de aanroeper mag niet blijven wachten
            log.exception("seek van %s", did)
            for req in (rt["seek_cur"], rt["seek_q"]):
                if req and not req["fut"].done():
                    req["fut"].set_exception(ModelError(f"seek mislukt: {e}"))
            rt["seek_q"] = None
        finally:
            if self.hub.tasks.get(f"seek:{did}") is asyncio.current_task():
                self.hub.tasks.pop(f"seek:{did}", None)

    async def _do_seek(self, did: str, rt: dict, req: dict) -> str:
        """Een seek sturen (de aanroeper houdt het slot vast). Dezelfde scène als de vorige en het apparaat houdt hem nog vast: alleen `{"t"}`; anders, of als het
        apparaat dat weigert ("geen scène om te tonen": zijn hold is verlopen of iets anders nam het over), met de bytes of het slot."""
        if did not in self.hub.online:
            raise _Fail("geen verbinding met het apparaat")
        tg, t = req["target"], req["t"]
        full = {"slot": tg["slot"]} if tg["slot"] else {"data": _b64(tg["data"])}
        run = rt["run"]
        quick = rt["seek_key"] == tg["key"] and bool(run) and run.get("seek") is not None and run["h"] == tg["h"]
        try:
            await self._post(did, "/huisdev/scene/seek", {"t": t} if quick else {**full, "t": t})
        except _Fail as e:
            if not quick or e.retry:
                raise
            await self._post(did, "/huisdev/scene/seek", {**full, "t": t})  # de terugval: het apparaat toonde niets meer
        shown = bool(rt["run"] and rt["run"].get("seek") is not None)
        rt["seek_key"] = tg["key"]
        rt["run"] = {"slot": tg["slot"] or 0, "h": tg["h"], "until": None, "seek": t}
        if not shown:
            self.hub._poke()
        return f"toont {t // 60000}:{t // 1000 % 60:02d}"

    async def seek_stop(self, did: str) -> dict:
        """Het vasthouden stoppen, maar alleen als het apparaat nu een beeld uit de tijdschuif toont (verse state): iets anders dat speelt blijft ongemoeid."""
        dev = self.hub.devices.get(did)
        if not dev:
            raise ModelError("onbekend apparaat")
        cap = dev["scn"]["cap"]
        if not cap or not cap.get("seek"):
            raise ModelError(NO_SEEK)
        if did not in self.hub.online:
            raise ModelError("niet verstuurd: geen verbinding met het apparaat")
        self._cancel_seek(did, "vervangen door een stop")
        rt = self.rt.setdefault(did, self._new())
        async with self._lock(did):
            await self._fresh(did)
            if not rt["run"] or rt["run"].get("seek") is None:
                return {"ok": True, "message": "het apparaat toont geen beeld uit de tijdschuif, niets te stoppen"}
            await self._post(did, "/huisdev/scene/stop", {})
            rt["run"] = None
        self.hub._poke()
        return {"ok": True, "message": "gestopt"}

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

    def _rule_timer(self, b: dict, r: dict) -> tuple[dict | None, int, str]:
        """(timer, minuten ervoor, reden): is de regel vooruit te berekenen? Alleen een regel met een enkele voorwaarde op een timer: `ingang = 1` of een wekker
        met een timer (die `before` minuten eerder begint). Anders None en de reden waarom niet."""
        m = self.hub.engine.model
        c = r["when"][0]
        inp = next((i for i in b["inputs"] if i["id"] == c.get("in")), None)
        if len(r["when"]) != 1:
            return None, 0, "geen geplande start: de regel heeft meer dan een voorwaarde (alleen een regel met enkel een timer is vooruit te berekenen), de gewone start blijft werken"
        if not inp or c["op"] not in ("on", "wake") or inp["kind"] != BOOL or not inp["ref"] or not inp["ref"].startswith("t:"):
            return None, 0, "geen geplande start: de bron is geen timer (alleen een timer is vooruit te berekenen), de gewone start blijft werken"
        t = m.timers.get(inp["ref"][2:])
        if t is None:
            return None, 0, "geen geplande start: de timer bestaat niet (meer)"
        return t, c.get("before", 0), ""

    def _desired_sched(self, did: str, dev: dict, rt: dict, cap: dict) -> list[dict] | None:
        """De geplande starts die dit apparaat zou moeten hebben, of None als er nog gewacht moet worden (een slot is nog niet gevuld of de klok is niet geldig)."""
        hub = self.hub
        now = self.wall()
        tz = getattr(hub.engine, "tz", None)
        bid = self.rules.device_owner().get(did)
        b = self.rules.items.get(bid) if bid else None
        want, waiting = [], False
        for sid in dev["scn"]["sel"]:
            n = dev["scn"]["slots"].get(sid)
            rules = [r for r in (b["rules"] if b else []) if r["scene"] == sid]
            if not rules or not n or sid not in self.lib.items:
                self._snote(rt, sid, None)
                continue
            reason = None
            for r in rules:
                t, before, why = self._rule_timer(b, r)
                if t is None:
                    reason = reason or why
                    continue
                if rt["have"].get(n, {}).get("h") != self.lib.hashes.get(sid):
                    waiting = True
                    continue
                want += [{"slot": n, "at": at - before * 60} for at in rising_edges(t, now + before * 60, cap["sched"], tz)]
            self._snote(rt, sid, reason)
        if waiting:
            return None
        uniq = sorted({(e["at"], e["slot"]) for e in want})[:cap["sched"]]
        return [{"slot": sl, "at": at} for at, sl in uniq]

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
            sel.append({"id": sid, "name": self.lib.name(sid), "slot": n, "release": self.lib.items[sid]["release"], "state": state, "note": last.get(KEY + sid, ""),
                        "sched_note": rt["snotes"].get(sid, "")})
        run = None
        if rt["run"]:
            r = rt["run"]
            sid = by_slot.get(r["slot"]) if r["slot"] else next((s for s, hh in self.lib.hashes.items() if hh == r["h"]), None)
            run = {"slot": r["slot"], "h": r["h"], "until": r["until"], "seek": r.get("seek"), "id": sid, "name": self.lib.name(sid) if sid else ""}
        sched = [{"slot": e["slot"], "at": e["at"], "id": by_slot.get(e["slot"]), "name": self.lib.name(by_slot[e["slot"]]) if e["slot"] in by_slot else ""} for e in rt["sched"]]
        return {"cap": cap, "known": rt["known"], "sel": sel, "have": {str(n): v for n, v in rt["have"].items()}, "run": run, "sched": sched, "time": rt["time"], "note": last.get("scenes", "")}

    def rules_view(self) -> list[dict]:
        """De blokken Scènes voor de pagina (engine.snapshot): ingangen met hun waarde nu, regels met wat er klopt, en wat de regels nu kiezen."""
        out = []
        for bid, b in self.rules.items.items():
            names = {i["id"]: i["name"] for i in b["inputs"]}
            note = self.rnote.get(bid) or {"idx": None, "unsure": False}
            dev = self.hub.devices.get(b["device"]) if b["device"] else None
            ins = [{**i, "value": self._read(i["ref"])} for i in b["inputs"]]
            rules = [{"scene": r["scene"], "scene_name": self.lib.name(r["scene"]), "when": r["when"], "text": [sr.condition_text(c, names) for c in r["when"]], "on": note["idx"] == n}
                     for n, r in enumerate(b["rules"])]
            idx = note["idx"]
            out.append({"id": bid, "name": b["name"], "device": b["device"], "device_name": dev["name"] if dev else "", "online": bool(dev and b["device"] in self.hub.online),
                        "ready": bool(dev and dev["scn"]["cap"]), "inputs": ins, "rules": rules, "inext": b["inext"], "active": b["rules"][idx]["scene"] if idx is not None else None,
                        "active_rule": idx, "unsure": note["unsure"]})
        return out
