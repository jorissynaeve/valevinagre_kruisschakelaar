"""Toegang: een logboek van detecties en deuren, bewaard in /data/access.json.

Een regel in het log komt van een punt van een HUISDEV-blok (`e:<id>`): een Reolink-camera (beweging, persoon, voertuig), een PIR of
bewegingsmelder (Hue of zelfbouw), een Nuki-slot (stand) of elk ander punt dat je zelf aanwijst (een deurcontact van een ESPHome-toestel).

  * **Handmatig.** Er staat niets in het log tot je een punt aanzet (`sources`). Daarna kijkt `tick()` elke seconde naar de waarde van
    die punten, onafhankelijk van het merk: alles wat een punt van een blok is, kan een bron zijn.
  * **Een bool is een periode.** Beweging begint (`start`) en eindigt (`end`); zolang ze loopt staat `end` op None. Een herstart houdt
    een lopende periode vast: is het punt dan uit, dan eindigt ze op dat moment.
  * **Een enum is een moment.** Een nieuwe stand van een slot (Ontgrendeld, Deur open…) is één regel met `text`, `start` is `end`.
  * **Onbekend is geen gebeurtenis.** Een punt dat offline gaat of nog niets meldde geeft nooit een regel, ook geen einde.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .engine import Engine

log = logging.getLogger(__name__)

MAX_LOG = 5000
KINDS = ("bool", "enum")  # wat een bron kan zijn: een periode of een stand


def _key(did: str, pid: str) -> str:
    return f"{did}:{pid}"


class AccessHub:
    def __init__(self, data_dir: Path, engine: Engine, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(data_dir) / "access.json"
        self.engine, self._clock = engine, clock
        self.espdev = None  # EspDevHub: de blokken met hun punten
        self.sources: list[str] = []  # "<blok>:<punt>" in de volgorde van aanzetten
        self.log: list[dict] = []
        self.seq = 0
        self.last: dict[str, object] = {}  # bron -> laatst bekende waarde (None: nog onbekend)
        self.open: dict[str, dict] = {}  # bron -> de regel van de periode die nu loopt
        self._load()

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
            self.sources = [k for k in dict.fromkeys(raw.get("sources") or []) if isinstance(k, str) and ":" in k]
            self.log = [e for e in raw.get("log") or [] if isinstance(e, dict) and isinstance(e.get("start"), (int, float))][-MAX_LOG:]
            self.seq = int(raw.get("seq") or 0)
            for e in self.log:
                if e.get("end") is None and e.get("key") in self.sources:
                    self.open[e["key"]] = e
                elif e.get("end") is None:
                    e["end"] = e["start"]  # van een bron die uit staat: niet eeuwig open laten
        except (TypeError, ValueError, AttributeError) as e:
            log.error("%s onleesbaar: %s", self.path, e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"sources": self.sources, "log": self.log[-MAX_LOG:], "seq": self.seq}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".access-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _poke(self) -> None:
        self.engine._emit("access", log=False)

    # ------------------------------------------------------------ bronnen

    def _point(self, key: str) -> tuple[str, dict, dict] | None:
        did, _, pid = key.partition(":")
        dev = self.espdev.devices.get(did) if self.espdev else None
        p = next((x for x in dev["points"] if x["id"] == pid), None) if dev else None
        return (did, dev, p) if p else None

    def candidates(self) -> list[dict]:
        """Alle punten die een bron kunnen zijn (bool of enum, geen diagnose), per blok, met of ze aanstaan."""
        out = []
        for did, dev in (self.espdev.devices.items() if self.espdev else []):
            for p in dev["points"]:
                if p["kind"] in KINDS and not p["diag"] and not p.get("gone"):
                    k = _key(did, p["id"])
                    out.append({"key": k, "device": dev["name"], "driver": dev["driver"], "label": p["label"], "kind": p["kind"], "on": k in self.sources})
        return out

    def set_sources(self, keys) -> None:
        if not isinstance(keys, list):
            raise ValueError("keys moet een lijst zijn")
        valid = {c["key"] for c in self.candidates()}
        new = [k for k in dict.fromkeys(keys) if k in valid]
        now = int(self._clock())
        for k in [k for k in self.sources if k not in new]:  # een bron die uitgaat: een lopende periode eindigt nu
            self._close(k, now)
            self.last.pop(k, None)
        self.sources = new
        self._save()
        self._poke()

    # ------------------------------------------------------------ bijhouden

    def _add(self, key: str, dev: dict, p: dict, start: int, end: int | None, text: str = "") -> dict:
        self.seq += 1
        e = {"id": self.seq, "key": key, "name": dev["name"], "label": p["label"], "kind": p["kind"], "start": start, "end": end, "text": text}
        self.log.append(e)
        if len(self.log) > MAX_LOG:
            self.log = self.log[-MAX_LOG:]
            keep = {id(x) for x in self.log}
            self.open = {k: v for k, v in self.open.items() if id(v) in keep}
        return e

    def _close(self, key: str, now: int) -> bool:
        e = self.open.pop(key, None)
        if e is None:
            return False
        e["end"] = max(now, e["start"])
        return True

    def _label(self, p: dict, v) -> str:
        return next((o["l"] for o in p.get("options") or [] if o["v"] == v), str(v))

    def tick(self) -> None:
        if not self.espdev or not self.sources:
            return
        now, changed = int(self._clock()), False
        for key in self.sources:
            found = self._point(key)
            if not found:
                continue
            did, dev, p = found
            v = self.espdev.point_value(did, p["id"])
            if v is None:  # offline of nog niet gemeld: geen gebeurtenis
                continue
            before = self.last.get(key)
            self.last[key] = v
            if p["kind"] == "bool":
                if v and key not in self.open:
                    self.open[key] = self._add(key, dev, p, now, None)
                    changed = True
                elif not v and key in self.open:
                    changed = self._close(key, now) or changed
            elif before is not None and v != before:
                self._add(key, dev, p, now, now, f"{self._label(p, before)} → {self._label(p, v)}")
                changed = True
        if changed:
            self._save()
            self._poke()

    # ------------------------------------------------------------ voor de pagina

    def view(self) -> dict:
        return {"events": list(self.log), "sources": self.candidates(),
                "missing": [k for k in self.sources if self._point(k) is None]}

    def clear(self) -> None:
        self.log = [e for e in self.log if e.get("key") in self.open and self.open.get(e["key"]) is e]
        self._save()
        self._poke()

    def csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["apparaat", "punt", "start", "einde", "duur (s)", "melding"])
        stamp = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
        for e in sorted(self.log, key=lambda e: e["start"]):
            end = e["end"]
            w.writerow([e["name"], e["label"], stamp(e["start"]), "" if end is None else stamp(end), "" if end is None else end - e["start"], e["text"]])
        return buf.getvalue()
