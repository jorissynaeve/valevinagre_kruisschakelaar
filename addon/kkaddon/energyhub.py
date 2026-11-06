"""Het energiedashboard van Home Assistant invullen vanuit de Huisconfig.

HA heeft al een energiedashboard (/energy). Wat het mist is één plek om voor veel huisjes tegelijk te zeggen welke meter wat is.
Hier kies je per rij een kWh-sensor (een Tuya-stroommeter, een omvormer, een accu) en zijn rol; de add-on schrijft dat met
`energy/save_prefs` in het dashboard van HA:

  house     een huisje: telt mee in het net (afname) én staat als apparaat in het overzicht, zodat je alle huizen naast elkaar ziet
  grid_in   afname van het net            grid_out   teruglevering aan het net
  solar     opbrengst van zonnepanelen    battery    een accu (ontladen = de sensor, laden = `entity_to`)
  device    een los apparaat (laadpaal, airco, ...)

Prijzen: een vast bedrag per kWh, of een sensor met de prijs (bv. een dynamisch tarief). Per rij, of voor alles in `price_in` en
`price_out`. Zonder prijs rekent HA niet.

Het dashboard van HA is van iedereen: de add-on past alleen zijn eigen regels aan. Wat je daar zelf toevoegt, blijft staan; wat de
add-on eerder schreef en je hier weghaalt, verdwijnt weer (`managed` in /data/energy.json onthoudt welke entiteiten dat waren).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .model import ModelError

log = logging.getLogger("kk.energy")

ROLES = {"house": "Huis", "grid_in": "Net: afname", "grid_out": "Net: teruglevering", "solar": "Zon", "battery": "Accu", "device": "Apparaat"}
MAX_ROWS = 80
ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]{1,100}$")
PRICE_ENTITY_RE = re.compile(r"^(sensor|input_number|number)\.[a-z0-9_]{1,100}$")
MAX_PRICE = 10.0  # €/kWh: daarboven is het een typfout
STATE_CLASSES = ("total", "total_increasing")


def parse_price(raw, what: str = "prijs"):
    """None (geen), een bedrag in €/kWh, of de entiteit van een sensor die de prijs geeft."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ModelError(f"{what}: een bedrag of een sensor")
    if isinstance(raw, (int, float)):
        val = float(raw)
    else:
        s = str(raw).strip()
        if not s:
            return None
        if PRICE_ENTITY_RE.match(s):
            return s
        try:
            val = float(s.replace(",", "."))
        except ValueError:
            raise ModelError(f"{what}: een bedrag in € per kWh (bv. 0,31) of een sensor (bv. sensor.stroomprijs)") from None
    if not 0 <= val <= MAX_PRICE:
        raise ModelError(f"{what}: een bedrag tussen 0 en {MAX_PRICE:g} € per kWh")
    return round(val, 5)


def _entity(raw, what: str) -> str:
    s = str(raw or "").strip()
    if not ENTITY_RE.match(s):
        raise ModelError(f"{what}: kies een sensor (bv. sensor.huis_1_energie_totaal)")
    return s


def clean(raw: dict) -> dict:
    """De ingestuurde instellingen controleren en in vaste vorm zetten."""
    if not isinstance(raw, dict):
        raise ModelError("ongeldige energie-instellingen")
    rows_in = raw.get("rows") or []
    if not isinstance(rows_in, list) or len(rows_in) > MAX_ROWS:
        raise ModelError(f"hoogstens {MAX_ROWS} rijen")
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, r in enumerate(rows_in, 1):
        if not isinstance(r, dict) or r.get("role") not in ROLES:
            raise ModelError(f"rij {i}: kies een rol")
        role = r["role"]
        row = {"role": role, "entity": _entity(r.get("entity"), f"rij {i}"), "name": str(r.get("name") or "").strip()[:60]}
        if (role, row["entity"]) in seen:
            raise ModelError(f"rij {i}: {row['entity']} staat al met deze rol in de lijst")
        seen.add((role, row["entity"]))
        if role == "battery":
            row["entity_to"] = _entity(r.get("entity_to"), f"rij {i} (accu laden)")
        elif role in ("house", "grid_in") and str(r.get("entity_to") or "").strip():  # de teruglevering van dezelfde aansluiting
            row["entity_to"] = _entity(r.get("entity_to"), f"rij {i} (teruglevering)")
        if role in ("house", "grid_in", "grid_out"):
            row["price"] = parse_price(r.get("price"), f"rij {i}")
        rows.append(row)
    return {"rows": rows, "price_in": parse_price(raw.get("price_in"), "prijs afname"), "price_out": parse_price(raw.get("price_out"), "prijs teruglevering")}


def entities(cfg: dict) -> set[str]:
    """Alle sensoren die in de instellingen voorkomen."""
    out: set[str] = set()
    for r in cfg["rows"]:
        out.add(r["entity"])
        if r.get("entity_to"):
            out.add(r["entity_to"])
    return out


def _price_fields(price) -> dict:
    return {"entity_energy_price": price if isinstance(price, str) else None, "number_energy_price": price if isinstance(price, float) else None}


def _is_legacy(prefs: dict) -> bool | None:
    """True als HA het oude netformaat (flow_from/flow_to) heeft, False bij het nieuwe (een bron per aansluiting), None als er nog geen netbron is."""
    for s in prefs.get("energy_sources") or []:
        if s.get("type") == "grid":
            return "flow_from" in s
    return None


def merge(prefs: dict, cfg: dict, managed: set[str], legacy: bool = False) -> dict:
    """De voorkeuren die in HA moeten komen: wat er stond, zonder onze oude en nieuwe regels, plus de nieuwe regels.

    Nieuwe HA: elke aansluiting (huis, afname, teruglevering) is een eigen netbron met naam, afname, teruglevering en prijzen.
    Oudere HA (`legacy`): één netbron met de lijsten flow_from en flow_to."""
    drop = entities(cfg) | managed
    sources = []
    grid = None
    for s in (dict(x) for x in prefs.get("energy_sources") or []):
        t = s.get("type")
        if t == "grid":
            if "flow_from" in s:  # oud formaat: één bron met lijsten
                grid = grid or s  # bij legacy komt het blok onderaan terug, met onze regels erbij; anders migreert HA het zelf
                continue
            elif s.get("stat_energy_from") in drop or s.get("stat_energy_to") in drop:
                continue
        elif t == "solar" and s.get("stat_energy_from") in drop:
            continue
        elif t == "battery" and (s.get("stat_energy_from") in drop or s.get("stat_energy_to") in drop):
            continue
        sources.append(s)
    devices = [d for d in prefs.get("device_consumption") or [] if d.get("stat_consumption") not in drop]
    flow_from = [f for f in (grid or {}).get("flow_from") or [] if f.get("stat_energy_from") not in drop]
    flow_to = [f for f in (grid or {}).get("flow_to") or [] if f.get("stat_energy_to") not in drop]
    for r in cfg["rows"]:
        role, ent, to = r["role"], r["entity"], r.get("entity_to")
        price_in = r.get("price") if r.get("price") is not None and role != "grid_out" else cfg["price_in"]
        price_out = r.get("price") if role == "grid_out" and r.get("price") is not None else cfg["price_out"]
        if role in ("house", "grid_in", "grid_out"):
            if legacy:
                if role != "grid_out":
                    flow_from.append({"stat_energy_from": ent, "stat_cost": None, **_price_fields(price_in)})
                for out in ([ent] if role == "grid_out" else [to] if to else []):
                    flow_to.append({"stat_energy_to": out, "stat_compensation": None, **_price_fields(price_out)})
            else:
                export = {"entity_energy_price_export": price_out if isinstance(price_out, str) else None, "number_energy_price_export": price_out if isinstance(price_out, float) else None}
                src = {"type": "grid", "stat_energy_from": None if role == "grid_out" else ent, "stat_energy_to": ent if role == "grid_out" else to or None,
                       "stat_cost": None, **_price_fields(None if role == "grid_out" else price_in), "stat_compensation": None, **export, "cost_adjustment_day": 0.0}
                if r["name"]:
                    src["name"] = r["name"]
                sources.append(src)
        if role in ("house", "device"):
            devices.append({"stat_consumption": ent, **({"name": r["name"]} if r["name"] else {})})
        if role == "solar":
            sources.append({"type": "solar", "stat_energy_from": ent, "config_entry_solar_forecast": None})
        if role == "battery":
            sources.append({"type": "battery", "stat_energy_from": ent, "stat_energy_to": to})
    if legacy:
        if grid is not None:
            grid["flow_from"], grid["flow_to"] = flow_from, flow_to
            sources.insert(0, grid)
        elif flow_from or flow_to:
            sources.insert(0, {"type": "grid", "flow_from": flow_from, "flow_to": flow_to, "cost_adjustment_day": 0.0})
    return {"energy_sources": sources, "device_consumption": devices}


class EnergyHub:
    def __init__(self, data_dir: Path, ha) -> None:
        self.path = Path(data_dir) / "energy.json"
        self.ha = ha
        self.cfg: dict = {"rows": [], "price_in": None, "price_out": None}
        self.managed: set[str] = set()  # wat we de vorige keer in HA zetten
        self.applied = ""  # laatste uitslag voor de UI
        self._load()

    # ------------------------------------------------------------ opslag

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.cfg = clean(raw)
            self.managed = {e for e in raw.get("managed") or [] if isinstance(e, str) and ENTITY_RE.match(e)}
        except FileNotFoundError:
            pass
        except (ValueError, OSError, ModelError, AttributeError) as e:
            log.error("%s niet leesbaar: %s", self.path, e)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({**self.cfg, "managed": sorted(self.managed)}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ voor de UI

    def candidates(self) -> list[dict]:
        """Sensoren in HA die een teller in kWh zijn: die kan het energiedashboard gebruiken."""
        out = []
        for ent, (state, attrs) in sorted(self.ha.states.items()):
            if ent.startswith("sensor.") and attrs.get("device_class") == "energy" and attrs.get("state_class") in STATE_CLASSES:
                out.append({"entity": ent, "name": attrs.get("friendly_name") or ent, "unit": attrs.get("unit_of_measurement") or "", "state": state})
        return out

    def prices(self) -> list[dict]:
        """Sensoren die een prijs kunnen geven (€/kWh), voor wie een dynamisch tarief heeft."""
        return [{"entity": e, "name": a.get("friendly_name") or e} for e, (_s, a) in sorted(self.ha.states.items())
                if e.startswith("sensor.") and "/kwh" in str(a.get("unit_of_measurement") or "").lower()]

    def view(self) -> dict:
        return {**self.cfg, "roles": ROLES, "candidates": self.candidates(), "prices": self.prices(), "ha": bool(self.ha.connected), "applied": self.applied}

    # ------------------------------------------------------------ bewerken

    def save(self, raw: dict) -> None:
        self.cfg = clean(raw)
        self._save()

    async def apply(self, raw: dict | None = None) -> dict:
        """Opslaan (als er instellingen zijn meegegeven) en in HA zetten. Geeft samengevat wat er nu staat."""
        if raw is not None:
            self.cfg = clean(raw)
            self._save()
        if not self.ha.connected:
            raise ModelError("Home Assistant is niet verbonden")
        try:
            prefs = await self.ha.energy_prefs()
        except Exception as e:
            raise ModelError(f"het energiedashboard van HA is niet te lezen (heeft de koppeling beheerdersrechten?): {e}") from e
        legacy = _is_legacy(prefs)
        order = [legacy] if legacy is not None else [False, True]  # zonder netbron weten we het niet: eerst het nieuwe formaat, dan het oude
        err: Exception | None = None
        for lg in order:
            new = merge(prefs, self.cfg, self.managed, legacy=lg)
            try:
                await self.ha.save_energy_prefs(new)
                break
            except Exception as e:
                err = err or e
        else:
            raise ModelError(f"HA weigert de energie-instellingen: {err}") from err
        self.managed = entities(self.cfg)
        self._save()
        srcs = new["energy_sources"]
        grids = [s for s in srcs if s.get("type") == "grid"]
        if grids and "flow_from" in grids[0]:
            n_in, n_out = len(grids[0]["flow_from"]), len(grids[0]["flow_to"])
        else:
            n_in, n_out = sum(1 for g in grids if g.get("stat_energy_from")), sum(1 for g in grids if g.get("stat_energy_to"))
        self.applied = (f"{n_in} afnamemeters, {n_out} terugleveringsmeters, {sum(1 for s in srcs if s['type'] == 'solar')} zon, "
                        f"{sum(1 for s in srcs if s['type'] == 'battery')} accu, {len(new['device_consumption'])} apparaten")
        log.info("energiedashboard in HA bijgewerkt: %s", self.applied)
        return {"summary": self.applied}
