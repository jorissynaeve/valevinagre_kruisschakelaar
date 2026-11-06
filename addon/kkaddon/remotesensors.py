"""De kWh-tellers van Tuya-toestellen bij een andere installatie als sensoren in de HA van deze installatie.

Wat je via *Andere installaties* kiest (bv. de stroommeter van een huisje in een ander subnet) is hier alleen een blok op het schema:
het energiedashboard van HA kent het niet. Voor elk gekozen Tuya-toestel van een andere installatie zet deze module daarom elk
datapunt in kWh als sensor in HA (via MQTT-discovery, klasse energie, `total_increasing`), zodat het in het energiedashboard past
(zie energyhub.py). Het apparaat in HA heet zoals het toestel daar, de entiteit `sensor.kk_remote_<installatie>_<toestel>_<dp>`.

De punten (naam, eenheid) worden onthouden in /data/remote_sensors.json: ligt de verbinding even weg of start de add-on opnieuw, dan
blijven de sensoren in HA staan (als niet beschikbaar) in plaats van te verdwijnen met hun geschiedenis. Alleen als je het toestel
bij *Andere installaties* niet meer kiest, gaan ze weg.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from . import discovery as d

log = logging.getLogger("kk.remote")

UNIT = "kwh"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")


class RemoteSensors:
    """Een extra voor de MQTT-brug (discovery, states, filters, command), net als TuyaHub."""

    filters: list[str] = []

    def __init__(self, data_dir: Path, link) -> None:
        self.path = Path(data_dir) / "remote_sensors.json"
        self.link = link
        self.seen: dict[str, dict[str, dict]] = {}  # installatie -> toestel -> {"name", "points": [{dp, name, unit}]}
        self._load()

    # ------------------------------------------------------------ opslag

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.seen = {p: o for p, o in raw.items() if isinstance(o, dict)}
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as e:
            log.error("%s niet leesbaar: %s", self.path, e)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.seen, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ wat er is

    def _devices(self) -> list[tuple[str, str, dict]]:
        """(installatie, toestel, {"name", "points"}) van elk gekozen Tuya-toestel bij een andere installatie, met de laatst bekende punten."""
        changed, out = False, []
        for peer in sorted(self.link.known):
            for obj in sorted(self.link.used(peer)):
                if not obj.startswith("y:"):
                    continue
                o = self.link.object(peer, obj) or {}
                pts = [{"dp": str(p.get("dp")), "name": str(p.get("name") or p.get("code") or p.get("dp")), "unit": p.get("unit")}
                       for p in o.get("points") or [] if str(p.get("unit") or "").lower() == UNIT and not p.get("hide")]
                if o.get("points") is not None:  # de verbinding is er: wat ze nu meldt is de waarheid
                    new = {"name": str(o.get("name") or obj), "points": pts}
                    if self.seen.get(peer, {}).get(obj) != new:
                        self.seen.setdefault(peer, {})[obj] = new
                        changed = True
                if obj in self.seen.get(peer, {}):
                    out.append((peer, obj, self.seen[peer][obj]))
        if changed:
            self._save()
        return out

    @staticmethod
    def _oid(peer: str, obj: str, dp: str) -> str:
        return f"kk_remote_{_slug(peer)}_{_slug(obj[2:])}_{_slug(dp)}"

    @staticmethod
    def t_state(peer: str, obj: str, dp: str) -> str:
        return f"{d.PREFIX}/remote/{_slug(peer)}/{_slug(obj[2:])}/{_slug(dp)}/state"

    @staticmethod
    def t_online(peer: str, obj: str) -> str:
        return f"{d.PREFIX}/remote/{_slug(peer)}/{_slug(obj[2:])}/online"

    # ------------------------------------------------------------ voor de MQTT-brug

    def discovery(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for peer, obj, dev in self._devices():
            device = {"identifiers": [f"kk_remote_{_slug(peer)}_{_slug(obj[2:])}"], "name": dev["name"], "manufacturer": "Tuya",
                      "model": f"Tuya bij {self.link.peer_name(peer)}"}
            for p in dev["points"]:
                oid = self._oid(peer, obj, p["dp"])
                out[f"{d.DISCOVERY}/sensor/{oid}/config"] = {
                    "device": device, "has_entity_name": True, "name": p["name"], "unique_id": oid, "default_entity_id": f"sensor.{oid}",
                    "state_topic": self.t_state(peer, obj, p["dp"]), "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing",
                    "availability_mode": "all", "availability": [{"topic": d.BRIDGE_STATUS}, {"topic": self.t_online(peer, obj)}]}
        return out

    def states(self) -> dict[str, tuple[str, bool]]:
        out: dict[str, tuple[str, bool]] = {}
        for peer, obj, dev in self._devices():
            live = self.link.object(peer, obj) or {}
            up = bool(self.link.is_up(peer) and live.get("online") and live.get("points") is not None)
            out[self.t_online(peer, obj)] = ("online" if up else "offline", True)
            if not up:
                continue  # de laatste waarde blijft retained staan
            vals = {str(p.get("dp")): p.get("value") for p in live.get("points") or []}
            for p in dev["points"]:
                v = vals.get(p["dp"])
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[self.t_state(peer, obj, p["dp"])] = (str(v), True)
        return out

    def command(self, topic: str, payload: str) -> bool:
        return False
