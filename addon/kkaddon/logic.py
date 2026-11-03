"""Kleine tijd- en logicamodules, om op het schema aan elkaar te koppelen.

Een module heeft een uitgang met een tijd (minuten sinds middernacht) of een 0/1. Een ingang wijst naar de uitgang van
een andere module (`m:<id>`), een timer (`t:<id>`), rechtstreeks naar een datapunt van een Tuya-toestel (`v:<datapunt>@<toestel>`) of naar een uitgang van een airco (`w:<id>:on|room|set`). Er is ook een derde waarde: None = onbekend, bv. een ingang die
niet is aangesloten of een zonsondergang zonder locatie. Onbekend blokkeert nooit iets.

De waarden worden niet bewaard maar telkens opnieuw berekend uit de klok, dus er is niets dat uit de pas kan lopen.
"""
from __future__ import annotations

import math
from datetime import date, datetime, tzinfo
from functools import lru_cache
from typing import Callable

DAY = 24 * 60
TIME, BOOL, NUMBER = "time", "bool", "number"

# type -> Nederlandse naam, soort uitgang en ingangen (naam, soort, label)
MODULE_TYPES: dict[str, dict] = {
    "sunrise": {"label": "Zonsopgang", "out": TIME, "inputs": ()},
    "sunset": {"label": "Zonsondergang", "out": TIME, "inputs": ()},
    "time": {"label": "Vaste tijd", "out": TIME, "inputs": ()},
    "number": {"label": "Getal", "out": NUMBER, "inputs": ()},
    "add": {"label": "Optellen", "out": TIME, "inputs": (("a", TIME, "tijd"), ("b", TIME, "plus"))},
    "sub": {"label": "Aftrekken", "out": TIME, "inputs": (("a", TIME, "tijd"), ("b", TIME, "min"))},
    "between": {"label": "Tussen", "out": BOOL, "inputs": (("from", TIME, "van"), ("to", TIME, "tot"))},
    "after": {"label": "Na", "out": BOOL, "inputs": (("time", TIME, "tijd"),)},
    "before": {"label": "Voor", "out": BOOL, "inputs": (("time", TIME, "tijd"),)},
    "and": {"label": "En", "out": BOOL, "inputs": (("a", BOOL, "A"), ("b", BOOL, "B"))},
    "or": {"label": "Of", "out": BOOL, "inputs": (("a", BOOL, "A"), ("b", BOOL, "B"))},
    "dp": {"label": "Tuya-waarde", "out": NUMBER, "inputs": ()},  # een datapunt van een Tuya-toestel, hier of bij een andere installatie
    "above": {"label": "Groter dan", "out": BOOL, "inputs": (("a", NUMBER, "waarde"),)},  # 1 als de waarde groter is dan de grens
    "below": {"label": "Kleiner dan", "out": BOOL, "inputs": (("a", NUMBER, "waarde"),)},
}

# de ingangen van een sprinkler: (naam, soort, label)
SPRINKLER_INPUTS = (("start", TIME, "startuur"), ("cycles", NUMBER, "cycli"), ("wait", NUMBER, "wachttijd"), ("block", BOOL, "blokkeren"))


def minutes(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:])


def disable_ref(d: str) -> str:
    """De disable van een lamp: `m:<id>` voor een module, een kaal id voor een timer (zo staat het al in oude bestanden)."""
    return d if d.startswith("m:") else f"t:{d}"


@lru_cache(maxsize=16)
def sun_times(day: date, lat: float, lon: float, tz: tzinfo | None) -> tuple[int, int] | None:
    """Zonsopgang en -ondergang van die dag in minuten lokale tijd (de zonsopgangsvergelijking, op een minuut of twee
    nauwkeurig). None als de zon die dag niet opkomt of niet ondergaat."""
    rad, deg = math.radians, math.degrees
    jstar = day.toordinal() + 1721425 - 2451545 + 0.0008 - lon / 360  # dagen sinds J2000 tot de middag ter plaatse
    m = rad((357.5291 + 0.98560028 * jstar) % 360)
    centre = 1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    ecl = rad((deg(m) + centre + 180 + 102.9372) % 360)
    transit = 2451545.0 + jstar + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * ecl)
    sin_d = math.sin(ecl) * math.sin(rad(23.4397))
    cos_d = math.cos(math.asin(sin_d))
    cos_w = (math.sin(rad(-0.833)) - math.sin(rad(lat)) * sin_d) / (math.cos(rad(lat)) * cos_d)
    if abs(cos_w) > 1:
        return None
    half = deg(math.acos(cos_w)) / 360

    def local(julian: float) -> int:
        t = datetime.fromtimestamp((julian - 2440587.5) * 86400 + 30, tz)  # +30 s: afronden op de minuut
        return t.hour * 60 + t.minute

    return local(transit - half), local(transit + half)


def _and(a: bool | None, b: bool | None) -> bool | None:
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def _or(a: bool | None, b: bool | None) -> bool | None:
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


def _between(now: int, a: int, b: int) -> bool:
    """Begin hoort erbij, einde niet. Is `van` later dan `tot`, dan loopt het over middernacht (zonsondergang tot zonsopgang)."""
    return a <= now < b if a <= b else now >= a or now < b


def evaluate(modules: dict[str, dict], timer: Callable[[str], bool | None], now: int, sun: tuple[int, int] | None,
             point: Callable[[str, str], float | None] | None = None, airco: Callable[[str], object] | None = None) -> dict:
    """De uitgang van elke module. Een lus of een verdwenen bron (kan alleen uit een oud bestand komen) geeft onbekend.
    `point(toestel, datapunt)`: de waarde van een Tuya-datapunt als getal, of None (offline, geen getal)."""
    values: dict[str, int | bool | None] = {}
    busy: set[str] = set()

    def read(ref: str | None):
        if ref is None:
            return None
        if ref.startswith("v:"):  # rechtstreeks een datapunt: v:<datapunt>@<toestel>
            dp, _, dev = ref[2:].partition("@")
            return point(dev, dp) if point else None
        if ref.startswith("w:"):  # een uitgang van een airco: w:<id>:on|room|set
            return airco(ref) if airco else None
        return timer(ref[2:]) if ref.startswith("t:") else module(ref[2:])

    def module(mid: str):
        if mid in values:
            return values[mid]
        if mid not in modules or mid in busy:
            return None
        busy.add(mid)
        values[mid] = compute(modules[mid])
        busy.discard(mid)
        return values[mid]

    def compute(m: dict):
        kind, ins = m["type"], m["inputs"]
        if kind == "sunrise":
            return sun[0] if sun else None
        if kind == "sunset":
            return sun[1] if sun else None
        if kind == "time":
            return minutes(m["value"])
        if kind == "number":
            return m["value"]
        if kind == "dp":
            return point(m["device"], m["dp"]) if point and m.get("device") and m.get("dp") else None
        if kind in ("and", "or"):
            return (_and if kind == "and" else _or)(read(ins.get("a")), read(ins.get("b")))
        args = [read(ins.get(name)) for name, _, _ in MODULE_TYPES[kind]["inputs"]]
        if None in args:
            return None
        if kind == "add":
            return (args[0] + args[1]) % DAY
        if kind == "sub":
            return (args[0] - args[1]) % DAY
        if kind == "between":
            return _between(now, args[0], args[1])
        if kind in ("above", "below"):
            return args[0] > m["value"] if kind == "above" else args[0] < m["value"]
        return now >= args[0] if kind == "after" else now < args[0]

    for mid in modules:
        module(mid)
    return values
