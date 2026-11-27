"""De tv-pagina: een alleen-lezen dashboard voor de tv (zie TV-OVERDRACHT.md), met de entiteiten gekoppeld op het tabblad TV.

De pagina (static/tv/display.*) is 16:9, zonder menu en zonder bediening: op de tv kun je niet navigeren. Het tv-blok (lghub.py) opent
hem als het dashboard. Wat erop staat, kies je hier:

  weer        een `weather.*`-entiteit, met de voorspelling per uur en het maximum en minimum van vandaag
  energie     verbruik, zonnepanelen, batterij en net, met een grafiek van de laatste uren
  tegels      tot 3 tegels met een titel en tot 4 meetwaarden (een zwembad, een kelder, ...), met een grafiek van één waarde
  klimaat     tot 4 temperaturen (de eerste twee groot), met een grafiek van twee waarden
  ophaalkalender  een `calendar.*`-entiteit; zonder kalender staat de plaats gereserveerd
  chips       tot 5 standen onderaan (eco-modus, alarm, ...), en een "volgende afspraak" uit een kalender
  meldingen   entiteiten die aan zijn geven "Let op" in de kop

Een bron is een HA-entiteit (`sensor.stroomverbruik`) of een uitgang van de add-on zelf (`o:deye:pv`, `w:airco:room`, `l:tv:watch`), dus een
omvormer of airco hoeft niet eerst in HA te staan. De tv kan niet via HA-ingress: de pagina draait op een eigen poort (`tv_port`) en is
alleen-lezen. Het adres bevat een geheim (`/tv/<geheim>/`), dat je op het tabblad kunt vernieuwen. Er staat nooit meer op dan wat je hier koos.

Grafieken: elke minuut een meetpunt in het geheugen (13 uur), bij het opstarten aangevuld uit de geschiedenis van HA.
Het bestand: /data/tv.json.

Twee bronnen buiten HA, allebei een adres dat jij opgeeft (de add-on haalt ze zelf op, met een time-out en een grootte-grens):

  weerstation  een JSON-adres met metingen (bv. een eigen weerstation): temperatuur, luchtdruk, luchtvochtigheid en een regenteller. Een meting is
               `{"time": ..., "value": ...}` of een kaal getal; een meting die ouder is dan `max_age` minuten telt als onbekend. De regenteller loopt op
               (mm sinds de installatie): de neerslag van vandaag is het verschil met middernacht, uit een meetpunt per 10 minuten in /data/tv_rain.json.
  ICS          een kalenderlink (.ics), bv. de agenda-export van een afvalophaler; elke afspraak is een ophaling. Geen herhalingsregels (RRULE).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .engine import Engine
from .model import ENTITY_RE, SOURCE_RE, ModelError

log = logging.getLogger("kk.tv")

STATIC = Path(__file__).parent / "static" / "tv"
ICON_NAMES = ("dot sun moon cloud partly rain pour snow bolt fog wind drop flask power pump home thermo flake fan cal bin leaf lock unlock bell door check warn solar "
              "battery zap waves flame car water gauge").split()  # zoals ICONS in static/tv/display.js (een test bewaakt dat)
MAX_TILES, MAX_ROWS, MAX_CHIPS, MAX_ALERTS = 3, 4, 5, 20
HISTORY_HOURS = 12.0
KEEP = 13 * 60  # meetpunten per bron (een per minuut)
SAMPLE_EVERY = 60.0
FORECAST_EVERY = 900.0
CALENDAR_EVERY = 1800.0
ON_STATES = {"on", "true", "home", "open", "unlocked", "active", "playing", "heat", "cool", "auto", "armed_home", "armed_away", "armed_night", "armed_vacation", "1"}
BAD_STATES = {"unavailable", "unknown", "none", ""}
SRC_RE = re.compile(rf"^({ENTITY_RE.pattern[1:-1]}|{SOURCE_RE.pattern[1:-1]})$")
DISPLAY_FILES = {"display.css": "text/css", "display.js": "application/javascript"}
WX_EVERY = 60.0  # s: het weerstation
ICS_EVERY = 6 * 3600.0  # s: de ophaalkalender verandert zelden
RAIN_EVERY = 600.0  # s tussen twee punten van de regenteller
RAIN_KEEP = 27 * 3600  # s
MAX_BODY = 2_000_000  # bytes van een bron buiten HA
URL_RE = re.compile(r"^https?://[^\s'\"<>]{1,400}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,60}$")


def _text(v, n: int, what: str = "tekst") -> str:
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise ModelError(f"{what}: tekst verwacht")
    return str(v).strip()[:n]


def _src(v, what: str) -> str:
    """Een bron: een HA-entiteit of een uitgang van de add-on (leeg mag)."""
    s = _text(v, 120, what)
    if s and not SRC_RE.match(s):
        raise ModelError(f"{what}: kies een HA-entiteit (bv. sensor.stroomverbruik) of een uitgang van een blok (bv. o:deye:pv)")
    return s


def _url(v, what: str) -> str:
    s = _text(v, 400, what)
    if s and not URL_RE.match(s):
        raise ModelError(f"{what}: een adres dat met http:// of https:// begint")
    return s


def _key(v, what: str, default: str) -> str:
    s = _text(v, 60, what) or default
    if not KEY_RE.match(s):
        raise ModelError(f"{what}: de naam van een veld in de JSON (bv. outTemp_C, of a.b voor een veld in een veld)")
    return s


def _icon(v, what: str = "pictogram") -> str:
    s = _text(v, 20, what)
    if s and s not in ICON_NAMES:
        raise ModelError(f"{what}: onbekend pictogram {s!r}")
    return s


def _num(v, what: str, lo: float, hi: float, default: float) -> float:
    if v in (None, ""):
        return default
    try:
        x = float(str(v).replace(",", "."))
    except ValueError:
        raise ModelError(f"{what}: een getal") from None
    if not lo <= x <= hi:
        raise ModelError(f"{what}: tussen {lo:g} en {hi:g}")
    return x


def _row(b, what: str) -> dict:
    if not isinstance(b, dict):
        raise ModelError(f"{what}: object verwacht")
    return {"label": _text(b.get("label"), 30, f"{what} naam"), "source": _src(b.get("source"), f"{what} bron"), "icon": _icon(b.get("icon"), f"{what} pictogram"),
            "unit": _text(b.get("unit"), 8, f"{what} eenheid"), "dec": int(_num(b.get("dec"), f"{what} decimalen", 0, 3, 1)),
            "on_text": _text(b.get("on_text"), 20, f"{what} tekst bij aan"), "off_text": _text(b.get("off_text"), 20, f"{what} tekst bij uit")}


def _list(b, n: int, what: str) -> list:
    if b in (None, ""):
        return []
    if not isinstance(b, list) or len(b) > n:
        raise ModelError(f"{what}: een lijst van hoogstens {n}")
    return b


def defaults() -> dict:
    return {"token": secrets.token_urlsafe(16), "title": "Mijn huis", "photo": "", "weather": "", "alerts": [],
            "weather_json": {"url": "", "temp": "outTemp_C", "pressure": "barometer_mbar", "humidity": "outHumidity", "rain_total": "rainTotal", "max_age": 60},
            "energy": {"consumption": "", "max": 5.0, "solar": "", "battery": "", "battery_kwh": "", "grid": ""},
            "tiles": [], "climate": {"title": "Temperaturen", "rows": [], "chart": []},
            "waste": {"on": True, "calendar": "", "ics": "", "days": 21}, "chips": [], "next": {"label": "Volgende afspraak:", "calendar": ""}}


def clean(b: dict, token: str) -> dict:
    """Controleert wat de pagina stuurt; wat niet klopt, geeft een duidelijke fout. Het geheim komt nooit uit `b`."""
    out = defaults()
    out["token"] = token
    out["title"] = _text(b.get("title"), 40, "titel") or "Mijn huis"
    photo = _text(b.get("photo"), 300, "foto")
    if photo and not re.match(r"^(https?://|/)[^\s'\"<>]+$", photo):
        raise ModelError("foto: een adres dat met http://, https:// of / begint")
    out["photo"] = photo
    w = _src(b.get("weather"), "weer")
    if w and not w.startswith("weather."):
        raise ModelError("weer: kies een entiteit van het soort weather.*")
    out["weather"] = w
    wj = b.get("weather_json") if isinstance(b.get("weather_json"), dict) else {}
    d0 = defaults()["weather_json"]
    out["weather_json"] = {"url": _url(wj.get("url"), "weerstation"), **{k: _key(wj.get(k), f"weerstation {k}", d0[k]) for k in ("temp", "pressure", "humidity", "rain_total")},
                           "max_age": int(_num(wj.get("max_age"), "weerstation maximale ouderdom (minuten)", 5, 1440, 60))}
    out["alerts"] = [_src(a, "melding") for a in _list(b.get("alerts"), MAX_ALERTS, "meldingen") if a]
    e = b.get("energy") if isinstance(b.get("energy"), dict) else {}
    out["energy"] = {k: _src(e.get(k), f"energie {k}") for k in ("consumption", "solar", "battery", "battery_kwh", "grid")}
    out["energy"]["max"] = _num(e.get("max"), "energie maximum", 0.5, 100, 5.0)
    out["tiles"] = [{"id": f"t{i + 1}", "title": _text(t.get("title"), 30, "tegel titel") or f"Tegel {i + 1}", "icon": _icon(t.get("icon")) or "waves",
                     "tag": _src(t.get("tag"), "tegel label"), "chart": _src(t.get("chart"), "tegel grafiek"),
                     "rows": [_row(r, "tegel meetwaarde") for r in _list(t.get("rows"), MAX_ROWS, "meetwaarden")]}
                    for i, t in enumerate(_list(b.get("tiles"), MAX_TILES, "tegels")) if isinstance(t, dict)]
    c = b.get("climate") if isinstance(b.get("climate"), dict) else {}
    out["climate"] = {"title": _text(c.get("title"), 30, "klimaat titel") or "Temperaturen", "rows": [_row(r, "temperatuur") for r in _list(c.get("rows"), MAX_ROWS, "temperaturen")],
                      "chart": [_src(x, "klimaat grafiek") for x in _list(c.get("chart"), 2, "grafiek") if x]}
    wa = b.get("waste") if isinstance(b.get("waste"), dict) else {}
    cal = _src(wa.get("calendar"), "ophaalkalender")
    if cal and not cal.startswith("calendar."):
        raise ModelError("ophaalkalender: kies een entiteit van het soort calendar.*")
    out["waste"] = {"on": bool(wa.get("on", True)), "calendar": cal, "ics": _url(wa.get("ics"), "kalenderlink (ICS)"), "days": int(_num(wa.get("days"), "dagen vooruit", 3, 60, 21))}
    out["chips"] = [{k: v for k, v in _row(c_, "knop").items() if k in ("label", "source", "icon", "on_text", "off_text")} for c_ in _list(b.get("chips"), MAX_CHIPS, "knoppen") if isinstance(c_, dict)]
    n = b.get("next") if isinstance(b.get("next"), dict) else {}
    ncal = _src(n.get("calendar"), "volgende afspraak")
    if ncal and not ncal.startswith("calendar."):
        raise ModelError("volgende afspraak: kies een entiteit van het soort calendar.*")
    out["next"] = {"label": _text(n.get("label"), 30, "label") or "Volgende afspraak:", "calendar": ncal}
    return out


def _pick(obj, path: str):
    """Een veld in een JSON-object; `a.b` gaat een niveau dieper."""
    for k in path.split("."):
        if not isinstance(obj, dict) or k not in obj:
            return None
        obj = obj[k]
    return obj


def reading(obj, path: str, now: float, max_age_min: float) -> float | None:
    """Een meting uit de JSON van een weerstation: `{"time": ..., "value": ...}` of een kaal getal. Een meting die te oud is, telt als onbekend: een
    weerstation dat uitviel, mag niet vorige week als nu tonen."""
    v = _pick(obj, path)
    stamp = None
    if isinstance(v, dict):
        stamp, v = v.get("time"), v.get("value")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    if stamp is not None:
        try:
            t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
        if now - t > max_age_min * 60 or t - now > 3600:
            return None
    return float(v)


def parse_ics(text: str, tz=None) -> list[dict]:
    """De afspraken uit een ICS-bestand als `{"start": "JJJJ-MM-DD", "summary": ...}` (zoals `calendar.get_events` van HA). Geen herhalingsregels.
    Een afspraak met een tijd in UTC wordt naar de datum in `tz` omgezet; een geannuleerde afspraak telt niet."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]  # een regel die doorloopt
        else:
            lines.append(raw)
    out, ev = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            ev = {}
        elif ln == "END:VEVENT":
            if ev and ev.get("start") and ev.get("summary") and ev.get("status") != "CANCELLED":
                out.append({"start": ev["start"], "summary": ev["summary"]})
            ev = None
        elif ev is not None and ":" in ln:
            name, _, val = ln.partition(":")
            base = name.split(";")[0].upper()
            if base == "DTSTART":
                m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})(Z?))?$", val.strip())
                if not m:
                    continue
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                try:
                    day = datetime(y, mo, d)
                    if m.group(7) == "Z":
                        day = datetime(y, mo, d, int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=timezone.utc).astimezone(tz) if tz else day
                except ValueError:
                    continue
                ev["start"] = day.date().isoformat()
            elif base == "SUMMARY":
                ev["summary"] = val.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\N", " ").replace("\\\\", "\\").strip()
            elif base == "STATUS":
                ev["status"] = val.strip().upper()
    return out


def lan_ip() -> str:
    """Het adres van deze machine op het thuisnetwerk, voor het adres dat de tv opent. De add-on draait in het host-netwerk, dus dit is dat van Home Assistant.
    Een UDP-socket \"verbinden\" zoekt alleen de route; er gaat niets over het netwerk. Leeg als dat niet lukt: de pagina valt dan terug op het adres van de browser."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: bestaat niet, we sturen niets
            ip = s.getsockname()[0]
        return "" if ip.startswith(("0.", "127.")) else ip
    except OSError:
        return ""


def _is_ref(src: str) -> bool:
    """Een uitgang van een blok (`o:deye:pv`), geen HA-entiteit (`sensor.x`)."""
    return bool(re.match(r"^[a-z]:", src or ""))


def _ref_unit(ref: str) -> str:
    """De eenheid van een uitgang van een omvormer (W, kWh, %); andere blokken geven er geen mee."""
    if ref.startswith("o:"):
        from .solarhub import OUTPUTS
        name = ref.rsplit(":", 1)[-1]
        return next((u for k, _t, _l, u, _g in OUTPUTS if k == name), "")
    return ""


class TvHub:
    def __init__(self, data_dir: Path, engine: Engine, port: int = 47530) -> None:
        self.path = Path(data_dir) / "tv.json"
        self.engine = engine
        self.port = port
        self.ha = None  # HaBridge: standen, voorspelling, geschiedenis, kalenders
        self.changed = None
        self.cfg: dict = defaults()
        self.hist: dict[str, deque] = {}
        self.forecast: dict[str, Any] = {"hourly": [], "daily": [], "at": 0.0}
        self.events: dict[str, Any] = {"waste": [], "next": [], "at": 0.0}
        self.wx: dict[str, Any] = {"temp": None, "pressure": None, "humidity": None, "rain_total": None, "at": 0.0, "ok": None}  # het weerstation (buiten HA)
        self.ics: dict[str, Any] = {"events": [], "at": 0.0, "ok": None}
        self.rain: deque = deque()  # (tijd, regenteller) per 10 minuten, bewaard in /data/tv_rain.json
        self.rain_path = Path(data_dir) / "tv_rain.json"
        self.rain_saved = 0.0
        self.errors: dict[str, str] = {}  # bron -> waarom ze niet lukte, voor het tabblad
        self.tasks: list[asyncio.Task] = []
        self.runner: web.AppRunner | None = None
        self.listening = 0  # de poort waarop de tv-pagina echt luistert (0: uit)
        self._load()
        self._load_rain()

    # ------------------------------------------------------------ opslag

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            tok = raw.get("token")
            self.cfg = clean(raw, tok if isinstance(tok, str) and len(tok) >= 16 else secrets.token_urlsafe(16))
        except FileNotFoundError:
            self._save()
        except (ValueError, OSError, AttributeError, ModelError) as e:
            log.error("%s niet leesbaar (%s): begin met een lege tv-pagina", self.path, e)
            self.cfg = defaults()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # het geheim zit in het adres: niet voor iedereen leesbaar
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def update(self, b: dict) -> dict:
        self.cfg = clean(b, self.cfg["token"])
        self._save()
        self.forecast["at"] = self.events["at"] = self.wx["at"] = self.ics["at"] = 0.0  # een andere bron: opnieuw ophalen
        self._wake()
        if self.changed:
            self.changed()
        return self.cfg

    def renew_token(self) -> str:
        """Een nieuw geheim: het oude adres werkt niet meer (bv. na een gedeeld scherm). Het tv-blok moet dan het nieuwe adres krijgen."""
        self.cfg["token"] = secrets.token_urlsafe(16)
        self._save()
        return self.cfg["token"]

    def token_ok(self, given: str) -> bool:
        return hmac.compare_digest(str(given).encode(), self.cfg["token"].encode())

    # ------------------------------------------------------------ bronnen lezen

    def read(self, src: str) -> tuple[float | None, str, str | None, dict]:
        """(getal, eenheid, tekst, attributen) van een bron. Het getal is None als de bron geen getal is of onbekend; de tekst is de ruwe stand."""
        if not src:
            return None, "", None, {}
        if _is_ref(src):
            v = self.engine.ref_value(src)
            if isinstance(v, bool):
                return None, "", "on" if v else "off", {}
            return (float(v) if isinstance(v, (int, float)) else None), _ref_unit(src), (None if v is None else str(v)), {}
        st = self.ha.states.get(src) if self.ha else None
        if st is None:
            return None, "", None, {}
        state, attrs = st
        unit = str(attrs.get("unit_of_measurement") or "")
        if state.lower() in BAD_STATES:
            return None, unit, None, attrs
        try:
            return float(state), unit, state, attrs
        except ValueError:
            return None, unit, state, attrs

    def kw(self, src: str) -> tuple[float | None, str]:
        """Een vermogen in kW (een sensor in W wordt omgerekend)."""
        v, unit, _t, _a = self.read(src)
        if v is None:
            return None, "kW"
        return (v / 1000.0, "kW") if unit.strip() == "W" else (v, unit or "kW")

    def _on(self, text: str | None) -> bool:
        return (text or "").lower() in ON_STATES

    def _row(self, r: dict) -> dict:
        """Een meetwaarde voor de pagina: een getal met eenheid, of (voor een stand of met eigen teksten) een woord."""
        v, unit, text, attrs = self.read(r["source"])
        out = {"label": r["label"] or attrs.get("friendly_name") or r["source"], "icon": r["icon"] or "dot"}
        custom = bool(r["on_text"] or r["off_text"])
        if v is not None and not custom:
            out.update(value=v, unit=r["unit"] or unit, dec=r["dec"])
        elif text is None:
            out["text"] = "–"
        else:
            on = self._on(text) or (v is not None and v > 0)
            out.update(text=(r["on_text"] or "Aan") if on else (r["off_text"] or "Uit"), tone="on" if on else "")
        return out

    # ------------------------------------------------------------ geschiedenis

    def sources_for_charts(self) -> list[str]:
        c = self.cfg
        srcs = [c["energy"]["consumption"], c["energy"]["solar"], *c["climate"]["chart"], *[t["chart"] for t in c["tiles"]]]
        return [s for s in dict.fromkeys(srcs) if s]

    def sample(self, now: float | None = None) -> None:
        """Een meetpunt per bron per minuut, de ruwe waarde (de eenheid wordt pas bij het tonen omgerekend)."""
        now = time.time() if now is None else now
        for src in self.sources_for_charts():
            v = self.read(src)[0]
            if v is None:
                continue
            h = self.hist.setdefault(src, deque(maxlen=KEEP))
            if not h or now - h[-1][0] >= SAMPLE_EVERY - 1:
                h.append((now, v))

    # ------------------------------------------------------------ de gegevens voor de pagina

    def _alerts(self) -> list[str]:
        out = []
        for a in self.cfg["alerts"]:
            _v, _u, text, attrs = self.read(a)
            if self._on(text):
                out.append(str(attrs.get("friendly_name") or a))
        return out

    def _weather(self) -> dict | None:
        """Het weer: de stand en de voorspelling uit een `weather.*`-entiteit van HA, en de metingen van een eigen weerstation (die voor de temperatuur voorgaan)."""
        ent, w = self.cfg["weather"], self.wx
        st = self.ha.states.get(ent) if (ent and self.ha) else None
        station = self.cfg["weather_json"]["url"] and any(w[k] is not None for k in ("temp", "pressure", "humidity", "rain_total"))
        if st is None and not station:
            return None
        state, a = st if st is not None else ("", {})
        tz = getattr(self.engine, "tz", None)
        daily = (self.forecast["daily"] or [{}])[0]

        def num(x):
            return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None
        now = time.time()
        hours = []
        for h in self.forecast["hourly"] if st is not None else []:
            try:
                t = datetime.fromisoformat(str(h.get("datetime")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if t.timestamp() < now - 1800:
                continue
            local = t.astimezone(tz) if tz else t.astimezone()
            cond = str(h.get("condition") or "")
            if cond == "sunny" and self._night(local):
                cond = "clear-night"
            hours.append({"t": f"{local.hour:02d}u", "state": cond, "temp": num(h.get("temperature"))})
            if len(hours) == 5:
                break
        cond = ""
        if st is not None:
            cond = state if state.lower() not in BAD_STATES else "cloudy"
            if cond == "sunny" and self._night(self.engine.local_now()):
                cond = "clear-night"
        temp = w["temp"] if w["temp"] is not None else num(a.get("temperature"))
        return {"state": cond, "temp": temp, "feels": num(a.get("apparent_temperature")), "max": num(daily.get("temperature")), "min": num(daily.get("templow")),
                "pressure": w["pressure"], "humidity": w["humidity"] if w["humidity"] is not None else num(a.get("humidity")), "rain": self._rain_today(), "hours": hours}

    def _rain_today(self) -> float | None:
        """De neerslag sinds middernacht (mm): het verschil van de regenteller met het laatste punt van voor middernacht, of met het eerste punt dat we hebben.
        Een teller die terugvalt (een reset) telt als een nieuw begin. Onbekend zolang het weerstation geen teller gaf."""
        if self.wx["rain_total"] is None or not self.rain:
            return None
        mid = self.engine.local_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        pts = sorted(self.rain)  # chronologisch, ook als het bestand dat niet was
        base = next((p for p in reversed(pts) if p[0] <= mid), pts[0])
        total = self.wx["rain_total"]
        return round(max(0.0, total - base[1]), 1) if total >= base[1] else 0.0

    def _night(self, local: datetime) -> bool:
        sun = self.engine.sun_today()
        if not sun:
            return local.hour >= 21 or local.hour < 6
        m = local.hour * 60 + local.minute
        return not sun[0] <= m < sun[1]

    def _energy(self) -> dict | None:
        e = self.cfg["energy"]
        if not (e["consumption"] or e["solar"] or e["battery"] or e["grid"]):
            return None
        out: dict[str, Any] = {"max": e["max"], "series": {}}
        v, unit = self.kw(e["consumption"])
        if e["consumption"]:
            out["consumption"] = {"value": v, "unit": unit}
            out["series"]["consumption"] = self._kw_series(e["consumption"])
        if e["solar"]:
            v, unit = self.kw(e["solar"])
            out["solar"] = {"value": v, "unit": unit}
            out["series"]["solar"] = self._kw_series(e["solar"])
        if e["grid"]:
            v, unit = self.kw(e["grid"])
            out["grid"] = {"value": v, "unit": unit, "label": "Net"}
        if e["battery"]:
            pct = self.read(e["battery"])[0]
            kwh = self.read(e["battery_kwh"])[0] if e["battery_kwh"] else None
            out["battery"] = {"pct": pct, "detail": f"{kwh:.1f} kWh".replace(".", ",") if kwh is not None else ""}
        out["series"] = {k: v for k, v in out["series"].items() if v}
        return out

    def _kw_series(self, src: str) -> list[list[float]]:
        """De meetpunten van een vermogen, in kW (de punten van een sensor in W worden omgerekend, want de gauge en de grafiek tonen kW)."""
        unit = self.read(src)[1].strip()
        k = 0.001 if unit == "W" else 1.0
        return [[round(t, 1), round(v * k, 3)] for t, v in sorted(self.hist.get(src, ()), key=lambda p: p[0])]

    def _plain_series(self, src: str) -> list[list[float]]:
        return [[round(t, 1), round(v, 3)] for t, v in sorted(self.hist.get(src, ()), key=lambda p: p[0])] if src else []

    def _waste(self) -> dict | None:
        w = self.cfg["waste"]
        if not w["on"]:
            return None
        today = self.engine.local_now().date()
        items = []
        for ev in [*self.events["waste"], *self.ics["events"]]:
            d = _event_date(ev)
            if d is None or d < today:
                continue
            label = str(ev.get("summary") or "").strip()
            if label and not any(i["date"] == d.isoformat() and i["label"] == label for i in items):
                items.append({"date": d.isoformat(), "label": label[:30], "days": (d - today).days})
        items.sort(key=lambda i: (i["date"], i["label"]))
        return {"configured": bool(w["calendar"] or w["ics"]), "today": today.isoformat(), "items": items[:6]}

    def _next(self) -> dict | None:
        n = self.cfg["next"]
        if not n["calendar"]:
            return None
        now = self.engine.local_now()
        best = None
        for ev in self.events["next"]:
            start = _event_start(ev, now.tzinfo)
            if start is None or start < now - timedelta(minutes=5):
                continue
            if best is None or start < best[0]:
                best = (start, ev)
        if best is None:
            return None
        start, ev = best
        timed = "T" in str(ev.get("start") or "")
        when = f"{start:%H:%M}" if timed and start.date() == now.date() else f"{start:%d/%m}" + (f" {start:%H:%M}" if timed else "")
        return {"label": n["label"], "text": f"{str(ev.get('summary') or '').strip()[:40]} {when}".strip()}

    def data(self) -> dict:
        c = self.cfg
        alerts = self._alerts()
        ha_down = self.ha is not None and not self.ha.connected
        status = {"ok": not alerts and not ha_down, "alerts": alerts,
                  "text": "Geen verbinding met Home Assistant" if ha_down else (f"{len(alerts)} melding" + ("en" if len(alerts) != 1 else "") if alerts else "Alles OK")}
        tz = getattr(self.engine, "tz", None)
        tiles = []
        for t in c["tiles"]:
            if not t["rows"]:
                continue
            tag_v, tag_u, tag_t, _ = self.read(t["tag"]) if t["tag"] else (None, "", None, {})
            tiles.append({"id": t["id"], "title": t["title"], "icon": t["icon"], "rows": [self._row(r) for r in t["rows"]],
                          "tag": (f"{tag_v:.1f}{tag_u}".replace(".", ",") if tag_v is not None else (tag_t or "")) if t["tag"] else "",
                          "series": self._plain_series(t["chart"])})
        climate = None
        if c["climate"]["rows"]:
            climate = {"title": c["climate"]["title"], "rows": [self._row(r) for r in c["climate"]["rows"]], "series": [s for s in (self._plain_series(x) for x in c["climate"]["chart"]) if s]}
        chips = []
        for ch in c["chips"]:
            _v, _u, text, attrs = self.read(ch["source"])
            on = self._on(text) or (_v is not None and _v > 0)
            if text is None:
                shown, tone = "Onbekend", "warn"
            else:
                shown, tone = (ch["on_text"] or "Aan") if on else (ch["off_text"] or "Uit"), ("on" if on else "")
            chips.append({"label": ch["label"] or attrs.get("friendly_name") or ch["source"], "text": shown, "icon": ch["icon"] or "dot", "on": on, "tone": tone})
        return {"title": c["title"], "now": int(time.time()), "tz": getattr(tz, "key", "") or "", "photo": c["photo"], "status": status, "weather": self._weather(),
                "energy": self._energy(), "tiles": tiles, "climate": climate, "waste": self._waste(), "chips": chips, "next": self._next()}

    # ------------------------------------------------------------ verversen op de achtergrond

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._poke = asyncio.Event()
        self.tasks.append(loop.create_task(self._run()))
        if self.port:
            self.tasks.append(loop.create_task(self._serve()))

    def _wake(self) -> None:
        ev = getattr(self, "_poke", None)
        if ev is not None:
            ev.set()

    def close(self) -> None:
        for t in self.tasks:
            t.cancel()
        self.tasks.clear()

    async def _run(self) -> None:
        backfilled: set[str] = set()
        while True:
            try:
                self.sample()
                await self._refresh_web()
                if self.ha and self.ha.connected:
                    await self._backfill(backfilled)
                    await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - een fout in één ronde mag de pagina niet voorgoed stilleggen
                log.exception("tv-pagina verversen")
            self._poke.clear()
            try:
                await asyncio.wait_for(self._poke.wait(), SAMPLE_EVERY)
            except asyncio.TimeoutError:
                pass

    async def _backfill(self, done: set[str]) -> None:
        """Bij het opstarten (en bij een nieuwe bron) de laatste uren uit de geschiedenis van HA halen, zodat een grafiek niet leeg begint."""
        for src in self.sources_for_charts():
            if src in done or _is_ref(src):
                continue
            done.add(src)
            rows = await self.ha.history(src, HISTORY_HOURS)
            h = self.hist.setdefault(src, deque(maxlen=KEEP))
            have = {int(t // 600) for t, _v in h}
            merged = sorted([*(p for p in rows if int(p[0] // 600) not in have), *h], key=lambda p: p[0])
            h.clear()
            h.extend(merged)

    async def _refresh(self) -> None:
        now = time.monotonic()
        if self.cfg["weather"] and now - self.forecast["at"] > FORECAST_EVERY:
            self.forecast["at"] = now
            self.forecast["hourly"] = await self.ha.forecast(self.cfg["weather"], "hourly")
            self.forecast["daily"] = await self.ha.forecast(self.cfg["weather"], "daily")
        if now - self.events["at"] > CALENDAR_EVERY:
            self.events["at"] = now
            w, n = self.cfg["waste"], self.cfg["next"]
            self.events["waste"] = await self.ha.calendar_events(w["calendar"], w["days"]) if (w["on"] and w["calendar"]) else []
            self.events["next"] = await self.ha.calendar_events(n["calendar"], 7) if n["calendar"] else []

    async def _get(self, url: str, what: str) -> bytes | None:
        """Een adres buiten HA ophalen, met een time-out en een grens aan de grootte. Een fout komt in `errors` (voor het tabblad) en geeft None."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as http:
                async with http.get(url, headers={"User-Agent": "Huisconfig (tv-pagina)"}) as r:
                    if r.status != 200:
                        raise ValueError(f"HTTP {r.status}")
                    body = await r.content.read(MAX_BODY + 1)
                    if len(body) > MAX_BODY:
                        raise ValueError("antwoord te groot")
            self.errors.pop(what, None)
            return body
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as e:
            msg = str(e) or type(e).__name__
            if self.errors.get(what) != msg:
                log.info("%s niet opgehaald: %s", what, msg)
            self.errors[what] = msg
            return None

    async def _refresh_web(self) -> None:
        """De bronnen buiten HA: het weerstation (elke minuut) en de ophaalkalender (ICS, elke 6 uur). Ze hebben HA niet nodig."""
        now = time.monotonic()
        wj = self.cfg["weather_json"]
        if wj["url"] and now - self.wx["at"] >= WX_EVERY - 1:
            self.wx["at"] = now
            body = await self._get(wj["url"], "weerstation")
            try:
                obj = json.loads(body) if body is not None else None
            except ValueError:
                self.errors["weerstation"] = "geen geldige JSON"
                obj = None
            if isinstance(obj, dict):
                t = time.time()
                for k in ("temp", "pressure", "humidity", "rain_total"):
                    self.wx[k] = reading(obj, wj[k], t, wj["max_age"])
                self.wx["ok"] = True
                self._rain_sample(t)
            else:
                self.wx.update(temp=None, pressure=None, humidity=None, rain_total=None, ok=False)  # onbekend, niet de vorige meting
        elif not wj["url"]:
            self.wx.update(temp=None, pressure=None, humidity=None, rain_total=None, ok=None)
            self.errors.pop("weerstation", None)
        ics = self.cfg["waste"]["ics"]
        if ics and self.cfg["waste"]["on"] and now - self.ics["at"] >= ICS_EVERY:
            self.ics["at"] = now
            body = await self._get(ics, "ophaalkalender")
            if body is not None:
                self.ics["events"] = parse_ics(body.decode("utf-8", "replace"), getattr(self.engine, "tz", None))
                self.ics["ok"] = True
            else:
                self.ics["ok"] = False  # de vorige afspraken blijven staan: een kalender die even niet antwoordt, is niet leeg
        elif not ics:
            self.ics.update(events=[], ok=None)
            self.errors.pop("ophaalkalender", None)

    # ------------------------------------------------------------ de regenteller

    def _load_rain(self) -> None:
        try:
            raw = json.loads(self.rain_path.read_text(encoding="utf-8"))
            now = time.time()
            for t, v in raw:
                if isinstance(t, (int, float)) and isinstance(v, (int, float)) and now - t <= RAIN_KEEP:
                    self.rain.append((float(t), float(v)))
        except FileNotFoundError:
            pass
        except (ValueError, OSError, TypeError) as e:
            log.warning("%s niet leesbaar (%s): de regenteller begint opnieuw", self.rain_path, e)

    def _rain_sample(self, now: float) -> None:
        """Een punt per 10 minuten (en meteen bij een reset van de teller); bewaard, zodat een herstart de neerslag van vandaag niet kwijtraakt."""
        total = self.wx["rain_total"]
        if total is None:
            return
        while self.rain and now - self.rain[0][0] > RAIN_KEEP:
            self.rain.popleft()
        if self.rain and total < self.rain[-1][1]:
            self.rain.clear()  # de teller viel terug: een nieuw begin
        if not self.rain or now - self.rain[-1][0] >= RAIN_EVERY - 1:
            self.rain.append((now, total))
            self._save_rain(now)

    def _save_rain(self, now: float) -> None:
        if now - self.rain_saved < RAIN_EVERY - 1 and self.rain_saved:
            return
        self.rain_saved = now
        try:
            tmp = self.rain_path.with_suffix(".tmp")
            tmp.write_text(json.dumps([[round(t, 1), v] for t, v in self.rain]), encoding="utf-8")
            os.replace(tmp, self.rain_path)
        except OSError as e:
            log.warning("regenteller niet bewaard: %s", e)

    # ------------------------------------------------------------ de server voor de tv

    async def _serve(self) -> None:
        app = web.Application()
        routes(app, self, "/tv/{token}", check=True)

        async def slash(request: web.Request) -> web.Response:
            raise web.HTTPFound(f"/tv/{request.match_info['token']}/")  # de pagina zoekt display.css naast zich
        app.router.add_get("/tv/{token}", slash)
        self.runner = web.AppRunner(app, access_log=None)
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, "0.0.0.0", self.port)
            await site.start()
            self.listening = self.port
            log.info("tv-pagina op :%d", self.listening)
        except OSError as e:
            log.warning("tv-pagina niet gestart op poort %d: %s", self.port, e)
            return
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.runner.cleanup()

    # ------------------------------------------------------------ voor het tabblad

    def view(self) -> dict:
        return {"config": self.cfg, "port": self.port, "listening": self.listening, "ha": bool(self.ha and self.ha.connected), "host": lan_ip(),
                "errors": dict(self.errors), "station": {k: self.wx[k] for k in ("temp", "pressure", "humidity", "rain_total", "ok")}, "rain_today": self._rain_today(),
                "ics_count": len(self.ics["events"]), "ics_ok": self.ics["ok"]}

    def sources(self, blocks: list[dict]) -> dict:
        """Wat je op het tabblad kunt kiezen: HA-entiteiten per soort, en de uitgangen van blokken (airco, omvormer, tv, ...)."""
        ha = []
        if self.ha:
            for ent, (state, attrs) in sorted(self.ha.states.items()):
                if ent.split(".")[0] in ("sensor", "binary_sensor", "weather", "calendar", "input_boolean", "switch", "climate", "alarm_control_panel", "lock",
                                         "person", "input_number", "number", "cover", "fan", "light", "sun", "input_select", "select"):
                    ha.append([ent, str(attrs.get("friendly_name") or ent), str(attrs.get("unit_of_measurement") or ""), state])
        addon = [[o["ref"], f"{b['name']} · {o['label']}", str(o.get("unit") or ""), "" if o.get("value") is None else str(o["value"])]
                 for b in blocks for o in (b.get("all") or b.get("outs") or []) if o.get("ref")]
        return {"ha": ha, "addon": addon}


def _event_date(ev: dict):
    s = str(ev.get("start") or "")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _event_start(ev: dict, tz):
    s = str(ev.get("start") or "")
    try:
        if "T" in s:
            t = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return t.astimezone(tz) if tz else t.astimezone()
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        return None


# ---------------------------------------------------------------- routes (voor de tv-poort met een geheim, en voor de voorvertoning achter ingress)


def routes(app: web.Application, hub: TvHub, prefix: str, check: bool) -> None:
    def guard(request: web.Request) -> None:
        if check and not hub.token_ok(request.match_info.get("token", "")):
            raise web.HTTPNotFound()

    async def page(request: web.Request) -> web.Response:
        guard(request)
        return web.Response(text=(STATIC / "display.html").read_text(encoding="utf-8"), content_type="text/html", headers={"Cache-Control": "no-store"})

    async def asset(request: web.Request) -> web.Response:
        guard(request)
        name = request.match_info["file"]
        return web.Response(body=(STATIC / name).read_bytes(), content_type=DISPLAY_FILES[name], headers={"Cache-Control": "no-store"})

    async def data(request: web.Request) -> web.Response:
        guard(request)
        return web.json_response(hub.data(), headers={"Cache-Control": "no-store"})

    app.router.add_get(prefix + "/", page)
    app.router.add_get(prefix + "/{file:display\\.(css|js)}", asset)
    app.router.add_get(prefix + "/data", data)
