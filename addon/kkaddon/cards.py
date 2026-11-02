"""Kaarten voor het NSPanel: eigen blokken op het schema (`c:<id>`), met een draad aan een paneel of aan een rasterkaart.

  * Een kaart aan een **paneel** is een hoofdpagina; de volgorde staat op het paneel. Een screensaver-kaart is het
    startscherm, niet een pagina.
  * Een kaart aan een **rasterkaart** is daar een knop naar een subpagina, met een pijl terug.

Dit bestand is puur. `config` maakt van het model de configuratie die `panel.Panel` al kent (het formaat van
nspanel-lovelace-ui), `Live` geeft de standen die niet uit HA komen (lampen van de add-on, de laadpaal), en `from_yaml`
zet een bestaande YAML eenmalig om naar kaarten.

Lampen en de laadpaal lezen hun stand uit de add-on zelf en niet via een HA-entiteit: hun naam in HA staat niet vast,
en een toestel van een andere installatie (later) staat niet in deze HA.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from .link import split_ref
from .panel import DEFAULT_COLOR, ON_COLOR, PanelError, extract, rgb565
from .points import Points

log = logging.getLogger("kk.cards")

TYPES = {"grid": "Knoppen", "charger": "Laadpaal", "saver": "Screensaver", "power": "Energie", "qr": "Wifi (QR)"}
LAYOUTS = {"grid": "cardGrid", "list": "cardEntities"}
MAX_ITEMS = {"grid": 8, "list": 4}  # een raster met meer dan 6 wordt het brede raster; een lijst toont er 4
MAX_POWER = 6
LAMP_REF = re.compile(r"^(g:[a-z0-9_]{1,80}|l\d{1,5}\.\d{1,2}|r:[a-z0-9_]{1,40}:(g:[a-z0-9_]{1,80}|l\d{1,5}\.\d{1,2}))$")  # r:<installatie>:<lamp> = van een andere installatie
CARD_REF = re.compile(r"^c:[a-z0-9_]{1,40}$")
DEVICE_REF = re.compile(r"^(y:[a-z0-9_]{1,80}|r:[a-z0-9_]{1,40}:y:[a-z0-9_]{1,80})$")  # een Tuya-toestel hier of bij een andere installatie, voor een waarde
LEVEL_RGB = ([0, 255, 0], [255, 165, 0], [255, 0, 0])  # groen, oranje, rood: dezelfde kleuren als het sjabloon van een HA-sensor
DYN = "kk"  # het domein van een rij die zijn stand uit de add-on haalt: kk.<kaart>__<deel>

# De laadpaal (Feyree, zie tuyahub.FEYREE): welke datapunten de kaart leest en schrijft
DP_STATUS, DP_ENERGY, DP_POWER, DP_TIME, DP_RUN = "101", "25", "109", "120", "124"
RUN_ON, RUN_OFF = "OpenCharging", "CloseCharging"
# DeviceState van de lader. Gemeten is alleen `connect`; de rest is een gok op wat zo'n lader meldt. Wat we niet kennen tonen we zoals het komt.
STATUS = {
    "nl": {"connect": "Aangesloten", "charing": "Aan het laden", "charging": "Aan het laden", "free": "Vrij", "idle": "Vrij", "available": "Vrij",
           "wait": "Wacht", "waiting": "Wacht", "finish": "Klaar", "charge_end": "Klaar", "end": "Klaar", "fault": "Storing", "error": "Storing"},
    "en": {"connect": "Connected", "charing": "Charging", "charging": "Charging", "free": "Free", "idle": "Free", "available": "Free",
           "wait": "Waiting", "waiting": "Waiting", "finish": "Done", "charge_end": "Done", "end": "Done", "fault": "Fault", "error": "Fault"},
}
CHARGING = {"charing", "charging"}
CHARGE_COLOR = rgb565([0, 200, 80])
WORDS = {
    "nl": {"time": "Laadtijd", "energy": "Geladen", "price": "Prijs", "start": "Start", "stop": "Stop", "offline": "Lader offline", "offline_short": "offline", "none": "Geen lader gekozen",
           "ssid": "Netwerk", "password": "Wachtwoord", "missing": "Niet gevonden"},
    "en": {"time": "Charge time", "energy": "Charged", "price": "Price", "start": "Start", "stop": "Stop", "offline": "Charger offline", "offline_short": "offline", "none": "No charger chosen",
           "ssid": "Network", "password": "Password", "missing": "Not found"},
}


def _clean(s: Any) -> str:
    """Een ~ breekt het protocol van het scherm (het scheidt de velden)."""
    return str(s or "").replace("~", "-")


def _icon(name: str | None, default: str | None = None) -> str | None:
    n = name or default
    return f"mdi:{n}" if n else None


def wifi_code(ssid: str, password: str, security: str) -> str:
    def esc(s: str) -> str:
        return re.sub(r'([\\;,:"])', r"\\\1", s)
    if security == "nopass":
        return f"WIFI:S:{esc(ssid)};T:nopass;;"
    return f"WIFI:S:{esc(ssid)};T:{security};P:{esc(password)};;"


# ---------------------------------------------------------------- welke kaarten hangen waar


def pages_of(model, pid: str) -> tuple[str | None, list[str], list[str]]:
    """(screensaver, hoofdpagina's, subpagina's) van een paneel. Een subpagina is elke kaart die via een rasterknop bereikbaar is."""
    cards = model.cards
    order = [c for c in model.panels[pid]["cards"] if c in cards]
    saver = next((c for c in order if cards[c]["type"] == "saver"), None)
    pages = [c for c in order if cards[c]["type"] != "saver"]
    seen, hidden, todo = set(pages), [], list(pages)
    while todo:
        c = todo.pop(0)
        for it in cards[c].get("items", []) if cards[c]["type"] == "grid" else []:
            ref = it.get("ref") or ""
            sub = ref[2:] if ref.startswith("c:") else None
            if sub in cards and sub not in seen and cards[sub]["type"] != "saver":
                seen.add(sub)
                hidden.append(sub)
                todo.append(sub)
    return saver, pages, hidden


def users(model, cid: str) -> list[dict]:
    """Waar een kaart aan hangt: panelen (als pagina) en rasterkaarten (als subpagina)."""
    def page(p: dict) -> int:  # het paginanummer op het scherm: de screensaver telt niet mee
        pages = [c for c in p["cards"] if model.cards.get(c, {}).get("type") != "saver"]
        return pages.index(cid) if cid in pages else 0
    out = [{"key": f"p:{pid}", "name": p["name"], "i": page(p), "as": "page"} for pid, p in model.panels.items() if cid in p["cards"]]
    for gid, g in model.cards.items():
        if g["type"] == "grid":
            out += [{"key": f"c:{gid}", "name": g["name"], "i": i, "as": "sub"} for i, it in enumerate(g["items"]) if it.get("ref") == f"c:{cid}"]
    return out


def lamp_name(model, ref: str) -> str | None:
    if ref.startswith("g:"):
        g = model.groups.get(ref[2:])
        return g["name"] if g else None
    m = re.fullmatch(r"l(\d+)\.(\d+)", ref)
    l = model.lights.get((int(m[1]), int(m[2]))) if m else None
    return (l.name or f"{model.node_name(l.node)} relais {l.relay + 1}") if l else None


# ---------------------------------------------------------------- van kaarten naar de configuratie van het scherm


def config(model, pid: str) -> dict:
    """De configuratie van een paneel, in het formaat dat `panel.Panel` leest. PanelError als er geen pagina aan hangt."""
    p = model.panels[pid]
    lang = p["locale"]
    saver, pages, hidden = pages_of(model, pid)
    if not pages:
        raise PanelError("Hang minstens één kaart aan dit paneel (een screensaver alleen is geen pagina).")
    out = {"model": "eu", "locale": "nl_NL" if lang == "nl" else "en_US", "sleepTimeout": p["timeout"], "screenBrightness": p["bright"], "sleepBrightness": p["dim"],
           "cards": [_card(model, c, lang) for c in pages], "hiddenCards": [_card(model, c, lang) for c in hidden]}
    if saver:
        out["screensaver"] = _saver(model.cards[saver])
    return out


def _card(model, cid: str, lang: str) -> dict:
    c = model.cards[cid]
    base = {"title": _clean(c["name"]) or TYPES[c["type"]], "key": cid}
    t = c["type"]
    if t == "grid":
        return {**base, "type": LAYOUTS[c["layout"]], "entities": [_grid_item(model, cid, i, it) for i, it in enumerate(c["items"])]}
    if t == "charger":
        w = WORDS[lang]
        return {**base, "type": "cardEntities", "entities": [
            {"entity": f"{DYN}.{cid}__run", "icon": "mdi:ev-station"},
            {"entity": f"{DYN}.{cid}__time", "name": w["time"], "icon": "mdi:timer-outline"},
            {"entity": f"{DYN}.{cid}__energy", "name": w["energy"], "icon": "mdi:lightning-bolt"},
            {"entity": f"{DYN}.{cid}__price", "name": w["price"], "icon": "mdi:currency-eur"}]}
    if t == "power":
        return {**base, "type": "cardPower", "entities": [_power_item(it, cid, i) for i, it in enumerate(c["items"])] or [{"entity": "delete"}]}
    if t == "qr":
        w = WORDS[lang]
        rows = [{"entity": f"iText.{_clean(c['ssid'])}", "name": w["ssid"], "icon": "mdi:wifi"}]
        if c["security"] != "nopass":
            rows.append({"entity": f"iText.{_clean(c['password'])}", "name": w["password"], "icon": "mdi:key"})
        return {**base, "type": "cardQR", "qrCode": wifi_code(c["ssid"], c["password"], c["security"]), "entities": rows}
    # een screensaver als pagina (via een rasterknop) kan niet: die wordt overgeslagen bij het zoeken
    return {**base, "type": "cardGrid", "entities": [{"entity": "delete"}]}


def device_of(ref: str) -> str:
    """De verwijzing naar een Tuya-toestel op een kaart (`y:<id>` of `r:..:y:<id>`) -> wat points.Points verwacht."""
    return ref[2:] if ref.startswith("y:") else ref


def _grid_item(model, cid: str, i: int, it: dict) -> dict:
    name = _clean(it.get("name")) or None
    ref, ent = it.get("ref"), it.get("entity")
    if it.get("dp"):  # een waarde van een Tuya-toestel: de naam van het datapunt komt live mee (Live.value)
        return {"entity": f"{DYN}.{cid}__{i}", "name": name, "icon": _icon(it.get("icon"), "gauge")}
    if ref and ref.startswith("c:"):
        return {"entity": f"navigate.{ref[2:]}", "name": name, "icon": _icon(it.get("icon"))}
    if ref and ref.startswith("r:"):  # de naam komt live van de andere installatie (Live.value), want ze kan daar veranderen
        return {"entity": f"{DYN}.{cid}__{i}", "name": name, "icon": _icon(it.get("icon"), "lightbulb")}
    if ref:
        return {"entity": f"{DYN}.{cid}__{i}", "name": name or _clean(lamp_name(model, ref)) or ref, "icon": _icon(it.get("icon"), "lightbulb")}
    if ent:
        return {"entity": ent, "name": name, "icon": _icon(it.get("icon"))}
    return {"entity": "delete"}


def _power_item(it: dict, cid: str = "", i: int = 0) -> dict:
    if it.get("tuya"):  # een Tuya-datapunt: waarde en kleur komen live uit de add-on (Live.value)
        return {"entity": f"{DYN}.{cid}__p{i}", "name": _clean(it.get("name")) or None, "icon": _icon(it.get("icon"), "flash"), "speed": it.get("speed", 0)}
    if not it.get("entity"):
        return {"entity": "delete"}
    out = {"entity": it["entity"], "name": _clean(it.get("name")) or None, "icon": _icon(it.get("icon")), "speed": it.get("speed", 0)}
    lv = it.get("levels")
    if lv:  # groen onder de eerste grens, oranje onder de tweede, anders rood (zoals de kleur in de oude YAML)
        s = f"states('{it['entity']}') | float(0)"
        out["color"] = f'{{{{ iif({s} < {lv[0]}, "[0,255,0]", iif({s} < {lv[1]}, "[255,165,0]", "[255,0,0]")) }}}}'
    return out


def _saver(c: dict) -> dict:
    ents = []
    if c.get("weather"):
        fc = c.get("forecast") or c["weather"]
        ents = [{"entity": c["weather"]}, *[{"entity": fc, "type": i} for i in range(4)]]
    if c.get("temperature"):
        ents.append({"entity": c["temperature"], "icon": "mdi:home-thermometer-outline"})
    out: dict = {"entities": ents}
    for n in (1, 2):
        if c.get(f"status{n}"):
            out[f"statusIcon{n}"] = {"entity": c[f"status{n}"]}
    return out


# ---------------------------------------------------------------- standen uit de add-on zelf


class Live:
    """Rijen `kk.<kaart>__<deel>`: een lamp van de add-on (een kring of een licht op het netwerk) of de laadpaal.

    `engine` voor de lampen, `tuya` (TuyaHub) voor de laadpaal, `state(entity)` voor een HA-entiteit (de prijs per kWh), `link`
    (LinkHub) voor een lamp of lader van een andere installatie (`r:<installatie>:<object>`)."""

    def __init__(self, model, engine=None, tuya=None, state: Callable[[str], Any] | None = None, link=None) -> None:
        self.model, self.engine, self.tuya, self.link = model, engine, tuya, link
        self.state = state or (lambda _e: None)
        self.points = Points(tuya, link)

    def _remote(self, ref: str) -> tuple[str, str, dict | None] | None:
        hit = split_ref(ref)
        if hit is None:
            return None
        peer, obj = hit
        return peer, obj, (self.link.object(peer, obj) if self.link else None)

    def _parse(self, entity: str) -> tuple[dict, str, str] | None:
        cid, _, part = entity.removeprefix(f"{DYN}.").partition("__")
        c = self.model.cards.get(cid)
        return (c, cid, part) if c else None

    # ------------------------------------------------------------ lezen

    def value(self, entity: str, lang: str = "nl") -> dict | None:
        hit = self._parse(entity)
        if hit is None:
            return None
        c, cid, part = hit
        if c["type"] == "power" and part.startswith("p") and part[1:].isdigit():
            return self._power(c, int(part[1:]))
        if c["type"] == "grid" and part.isdigit():
            items = c["items"]
            i = int(part)
            if i < len(items) and items[i].get("dp") and items[i].get("ref"):
                return self._value(device_of(items[i]["ref"]), items[i]["dp"])
            if i >= len(items) or not items[i].get("ref") or items[i]["ref"].startswith("c:"):
                return None
            ref = items[i]["ref"]
            on = self.lamp_on(ref)
            out = {"kind": "switch", "value": 1 if on else 0, "color": ON_COLOR if on else DEFAULT_COLOR}
            if ref.startswith("r:"):
                r = self._remote(ref)
                o = r[2] if r else None
                if o is None:
                    return None
                out["name"] = _clean(o.get("name")) + ("" if o.get("online") else f" ({WORDS[lang]['offline_short']})")
            return out
        if c["type"] == "charger":
            return self._charger(c, part, WORDS[lang], lang)
        return None

    def _value(self, dev: str, dp: str) -> dict | None:
        """Een rij met een Tuya-datapunt: de waarde met eenheid, en de naam van het datapunt als de kaart er geen eigen heeft."""
        d = self.points.device(dev)
        if d is None:
            return None
        p = d["points"].get(str(dp))
        text = self.points.text(dev, dp) if p else None
        return {"kind": "text", "name": _clean(p.get("name")) if p else str(dp), "value": _clean(text) if d["online"] and text else "—", "color": DEFAULT_COLOR}

    def _power(self, c: dict, i: int) -> dict | None:
        if i >= len(c["items"]) or not c["items"][i].get("tuya"):
            return None
        it = c["items"][i]
        out = self._value(device_of(it["tuya"]), it["dp"])
        if out is None:
            return None
        n, lv = self.points.number(device_of(it["tuya"]), it["dp"]), it.get("levels")
        if lv and n is not None:  # groen onder de eerste grens, oranje onder de tweede, anders rood
            out["color"] = rgb565(LEVEL_RGB[0 if n < lv[0] else 1 if n < lv[1] else 2])
        return out

    def lamp_on(self, ref: str) -> bool | None:
        if ref.startswith("r:"):
            r = self._remote(ref)
            o = r[2] if r else None
            return o.get("on") if o and o.get("online") else None
        e = self.engine
        if e is None:
            return None
        if ref.startswith("g:"):
            ms = e.groups.members.get(ref[2:]) or []
            basis = [m for m in ms if m.lamp and m.known is not None] or ms
            return None if all(m.known is None for m in basis) else any(m.known for m in basis)
        m = re.fullmatch(r"l(\d+)\.(\d+)", ref)
        return e.light_state(int(m[1]), int(m[2])) if m else None

    def _points(self, c: dict) -> dict | None:
        """De gemelde waarden van de lader (zoals in de zijbalk: met de schaal toegepast), of None als hij er niet is."""
        did = c.get("tuya") or ""
        if did.startswith("r:"):
            r = self._remote(did)
            o = r[2] if r else None
            return {str(p.get("dp")): p.get("value") for p in o.get("points") or []} if o else None
        t = self.tuya
        if t is None or not did or did not in t.devices:
            return None
        return t.read(did)

    def _online(self, c: dict) -> bool:
        did = c.get("tuya") or ""
        if did.startswith("r:"):
            r = self._remote(did)
            return bool(r and r[2] and r[2].get("online") and self.link.is_up(r[0]))
        return self.tuya is not None and self.tuya.is_online(did)

    def _set(self, c: dict, dp: str, value: str) -> bool:
        """Een datapunt van de lader zetten: zelf, of via de installatie waar hij hangt (die beslist)."""
        did = c["tuya"]
        if did.startswith("r:"):
            r = self._remote(did)
            return bool(r and self.link and self.link.send(r[0], r[1], dp=dp, value=value))
        self.tuya.set(did, dp, value)
        return True

    def charging(self, c: dict) -> bool:
        v = self._points(c) or {}
        return v.get(DP_RUN) == RUN_ON or v.get(DP_STATUS) in CHARGING

    def _charger(self, c: dict, part: str, w: dict, lang: str) -> dict:
        v = self._points(c)
        online = v is not None and self._online(c)
        if part == "run":
            if v is None:
                return {"kind": "text", "name": w["none"] if not c.get("tuya") else w["offline"], "value": "", "color": DEFAULT_COLOR}
            if not online:
                return {"kind": "text", "name": w["offline"], "value": "", "color": DEFAULT_COLOR}
            on = self.charging(c)
            st = v.get(DP_STATUS)
            return {"kind": "button", "name": STATUS[lang].get(str(st), str(st)) if st is not None else "?", "value": w["stop"] if on else w["start"],
                    "color": CHARGE_COLOR if on else DEFAULT_COLOR}
        v = v or {}
        if part == "time":
            t = v.get(DP_TIME)
            return {"kind": "text", "value": _clean(t) if t not in (None, "") else "—"}
        energy = v.get(DP_ENERGY)
        if part == "energy":
            return {"kind": "text", "value": f"{energy:.2f} kWh" if isinstance(energy, (int, float)) else "—"}
        if part == "price":
            tariff = self.tariff(c)
            if not isinstance(energy, (int, float)) or tariff is None:
                return {"kind": "text", "value": "—"}
            return {"kind": "text", "value": f"€ {energy * tariff:.2f}"}
        return {"kind": "text", "value": ""}

    def tariff(self, c: dict) -> float | None:
        """De prijs per kWh: de HA-entiteit als ze een getal geeft, anders het vaste tarief."""
        if c.get("price_entity"):
            st = self.state(c["price_entity"])
            try:
                return float(st[0]) if st else c["price"]
            except (TypeError, ValueError):
                return c["price"]
        return c["price"]

    # ------------------------------------------------------------ bedienen

    def press(self, entity: str, kind: str, value: str | None) -> bool:
        hit = self._parse(entity)
        if hit is None:
            return False
        c, cid, part = hit
        if c["type"] == "grid" and part.isdigit() and int(part) < len(c["items"]):
            ref = c["items"][int(part)].get("ref") or ""
            if not LAMP_REF.match(ref):
                return False
            on = (value == "1") if kind == "OnOff" else None
            return self._lamp(ref, on)
        if c["type"] == "charger" and part == "run" and kind == "button":
            if self._points(c) is None or not self._online(c):
                log.info("laadpaal %s: niet bereikbaar, niets gestuurd", c["name"])
                return False
            want = RUN_OFF if self.charging(c) else RUN_ON
            try:
                if not self._set(c, DP_RUN, want):
                    return False
            except ValueError as e:  # ModelError
                log.warning("laadpaal %s: %s", c["name"], e)
                return False
            log.info("laadpaal %s: %s vanaf het paneel", c["name"], want)
            return True
        return False

    def _lamp(self, ref: str, on: bool | None) -> bool:
        if ref.startswith("r:"):  # de eigenaar beslist (bv. een lamp met een aan/uit-ingang weigert)
            r = self._remote(ref)
            return bool(r and self.link and self.link.send(r[0], r[1], op="toggle" if on is None else "on" if on else "off"))
        e = self.engine
        if e is None:
            return False
        if ref.startswith("g:"):
            gid = ref[2:]
            if e.groups.held(gid) is not None:
                log.info("lamp %s wordt vastgehouden door zijn aan/uit-ingang: druk op het paneel genegeerd", gid)
                return False
            return e.groups.command(gid, on)
        m = re.fullmatch(r"l(\d+)\.(\d+)", ref)
        n, r = int(m[1]), int(m[2])
        return e.toggle_light(n, r) if on is None else e.command_light(n, r, on)


# ---------------------------------------------------------------- eenmalig: een YAML van nspanel-lovelace-ui omzetten

LEVELS_RE = re.compile(r"<\s*([0-9.]+)")
WIFI_RE = re.compile(r"^WIFI:(.*);;$")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")[:30] or "kaart"


def _plain_icon(v: Any) -> str:
    return v.removeprefix("mdi:")[:60] if isinstance(v, str) and re.fullmatch(r"(mdi:)?[a-z0-9-]{1,60}", v) else ""


def _wifi(text: str) -> dict | None:
    m = WIFI_RE.match(text or "")
    if not m:
        return None
    fields: dict[str, str] = {}
    cur, esc, parts = "", False, []
    for ch in m[1]:
        if esc:
            cur, esc = cur + ch, False
        elif ch == "\\":
            esc = True
        elif ch == ";":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    for p in parts:
        k, _, v = p.partition(":")
        fields[k] = v
    sec = fields.get("T") or "nopass"
    return {"ssid": fields.get("S", ""), "password": fields.get("P", ""), "security": sec if sec in ("WPA", "WEP", "nopass") else "WPA"}


def from_yaml(source: Any, taken: set[str]) -> tuple[dict[str, dict], dict, list[str]]:
    """(nieuwe kaarten, velden voor het paneel, meldingen). `taken`: kaart-id's die al bestaan. Gooit PanelError als de YAML niet te lezen is.

    Wat niet om te zetten valt (sjablonen in een naam, voorwaarden, onbekende kaarttypes, ...) komt in de meldingen; de rest gaat mee."""
    cfg = extract(source)
    notes: list[str] = []
    used = set(taken)
    cards: dict[str, dict] = {}

    def new_id(title: str) -> str:
        base = _slug(title)
        cid, n = base, 2
        while cid in used:
            cid, n = f"{base}_{n}", n + 1
        used.add(cid)
        return cid

    raw_cards = [(c, False) for c in cfg.get("cards") or []] + [(c, True) for c in cfg.get("hiddenCards") or []]
    by_key: dict[str, str] = {}
    planned: list[tuple[dict, str, bool]] = []
    for raw, hidden in raw_cards:
        if not isinstance(raw, dict):
            continue
        cid = new_id(str(raw.get("title") or raw.get("type") or "kaart"))
        if raw.get("key"):
            by_key[str(raw["key"])] = cid
        planned.append((raw, cid, hidden))

    def plain(v: Any, what: str) -> str:
        if isinstance(v, str) and ("{{" in v or "{%" in v):
            notes.append(f"{what}: sjabloon {v!r} niet overgenomen")
            return ""
        return str(v)[:60] if v is not None else ""

    def item_notes(e: dict, title: str) -> None:
        for k in ("state", "state_not", "state_template", "status", "value", "type"):
            if k in e:
                notes.append(f"{title}: '{k}' bij {e.get('entity')} niet overgenomen")
        if "color" in e:
            notes.append(f"{title}: kleur bij {e.get('entity')} niet overgenomen")

    pages: list[str] = []
    for raw, cid, hidden in planned:
        typ, title = raw.get("type"), plain(raw.get("title"), "titel") or str(raw.get("type"))
        ents = [e for e in raw.get("entities") or [] if isinstance(e, dict) and isinstance(e.get("entity"), str)]
        if typ in ("cardGrid", "cardGrid2", "cardEntities"):
            items = []
            for e in ents:
                ent = e["entity"]
                name, icon = plain(e.get("name"), title), _plain_icon(e.get("icon"))
                if ent == "delete":
                    items.append({"ref": None, "entity": None, "name": "", "icon": ""})
                elif ent.startswith("navigate."):
                    target = ent.removeprefix("navigate.")
                    if target in by_key:
                        items.append({"ref": f"c:{by_key[target]}", "entity": None, "name": name, "icon": icon})
                    else:
                        notes.append(f"{title}: knop naar {target} niet gevonden, weggelaten")
                elif ent.startswith(("iText.", "service.")):
                    notes.append(f"{title}: {ent} kan (nog) niet op een knoppenkaart, weggelaten")
                else:
                    item_notes(e, title)
                    items.append({"ref": None, "entity": ent, "name": name, "icon": icon})
            layout = "list" if typ == "cardEntities" else "grid"
            if len(items) > MAX_ITEMS[layout]:
                notes.append(f"{title}: maar {MAX_ITEMS[layout]} knoppen passen, de rest is weggelaten")
                items = items[:MAX_ITEMS[layout]]
            cards[cid] = {"type": "grid", "name": title, "layout": layout, "items": items}
        elif typ == "cardQR":
            w = _wifi(str(raw.get("qrCode") or ""))
            if w is None:
                notes.append(f"{title}: alleen een wifi-QR-code kan worden overgenomen, deze kaart is weggelaten")
                continue
            cards[cid] = {"type": "qr", "name": title, **w}
        elif typ == "cardPower":
            items = []
            for e in ents[:MAX_POWER]:
                if e["entity"] == "delete":
                    items.append({"entity": None, "name": "", "icon": "", "speed": 0, "levels": None})
                    continue
                levels = None
                col = e.get("color")
                if isinstance(col, str):
                    found = [float(x) for x in LEVELS_RE.findall(col)]
                    if len(found) >= 2 and found[0] < found[1]:
                        levels = found[:2]
                    else:
                        notes.append(f"{title}: kleur bij {e['entity']} niet overgenomen")
                speed = e.get("speed", 0)
                if not isinstance(speed, int) or isinstance(speed, bool) or not -120 <= speed <= 120:
                    notes.append(f"{title}: snelheid {speed!r} bij {e['entity']} niet overgenomen")
                    speed = 0
                items.append({"entity": e["entity"], "name": plain(e.get("name"), title), "icon": _plain_icon(e.get("icon")), "speed": speed, "levels": levels})
            cards[cid] = {"type": "power", "name": title, "items": items}
        else:
            notes.append(f"kaart '{title}' ({typ}) kan niet worden omgezet en is weggelaten")
            continue
        if not hidden:
            pages.append(cid)

    sc = cfg.get("screensaver") or {}
    saver = {"type": "saver", "name": "Screensaver", "weather": None, "forecast": None, "temperature": None, "status1": None, "status2": None}
    for e in sc.get("entities") or ([{"entity": sc["entity"]}] if isinstance(sc.get("entity"), str) else []):
        if not isinstance(e, dict) or not isinstance(e.get("entity"), str):
            continue
        ent = e["entity"]
        if "type" in e:
            saver["forecast"] = saver["forecast"] or ent
        elif saver["weather"] is None and (ent.startswith("weather.") or ent.endswith("forecast_daily")):
            saver["weather"] = ent
        else:
            saver["temperature"] = ent
    if saver["forecast"] == saver["weather"]:
        saver["forecast"] = None
    for key in ("tapToWake", "doubleTapToUnlock", "inNavigation", "defaultCard", "theme", "sleepTimeout"):
        if key in sc:
            notes.append(f"screensaver: {key} niet overgenomen")
    for n in (1, 2):
        s = sc.get(f"statusIcon{n}")
        if isinstance(s, dict) and isinstance(s.get("entity"), str):
            saver[f"status{n}"] = s["entity"]
    order = pages
    if saver["weather"] or saver["temperature"]:
        sid = new_id("screensaver")
        cards[sid] = saver
        order = [sid, *pages]

    panel: dict = {"cards": order}
    if cfg.get("locale"):  # zonder locale toonde nspanel-lovelace-ui alles in het Engels: dat beslist wie dit aanroept
        panel["locale"] = "nl" if str(cfg["locale"]).lower().startswith("nl") else "en"
    for key, field, lo, hi in (("sleepTimeout", "timeout", 0, 3600), ("screenBrightness", "bright", 0, 100), ("sleepBrightness", "dim", 0, 100)):
        v = cfg.get(key)
        if v is None:
            continue
        if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
            panel[field] = v
        else:
            notes.append(f"{key}: {v!r} niet overgenomen (alleen een vast getal)")
    for key in ("dateFormat", "timeFormat", "dateAdditionalTemplate", "timeAdditionalTemplate", "defaultBackgroundColor", "featureExperimentalSliders"):
        if cfg.get(key) not in (None, "", False):
            notes.append(f"{key} niet overgenomen")
    return cards, panel, notes
