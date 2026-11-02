"""Het NSPanel-scherm: de configuratie (hetzelfde YAML-formaat als nspanel-lovelace-ui) en de standen uit HA worden de
tekstcommando's voor de Nextion-TFT, en wat er op het scherm gebeurt wordt een HA-bevel.

Pure logica, zonder I/O: `Panel` krijgt een `Env` (standen, templates, tijd, bevelen) en een `send(tekst)`.
Het protocol is dat van de bestaande TFT (zie DESIGN.md §26), zodat het scherm zelf niet opnieuw geflasht hoeft:

  naar het scherm   pageType~cardGrid                 pagina kiezen
                    entityUpd~titel~links~rechts~... inhoud van een pagina (7 velden per rij: soort~id~icoon~kleur~naam~waarde)
                    time~12:34~ / date~... / dimmode~.. / timeout~20 / weatherUpdate~... / statusUpdate~...
  van het scherm    event,startup,<versie>,<model>   event,sleepReached,<kaart>
                    event,buttonPress2,<id>,<soort>[,<waarde>]
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol

import yaml

from .panel_icons import ICONS

log = logging.getLogger("kk.panel")

DEFAULT_COLOR = 17299  # [68,115,158] in RGB565
WHITE = 65535
UNAVAILABLE = ("unavailable", "unknown")
CARD_TYPES = ("cardGrid", "cardGrid1", "cardGrid2", "cardEntities", "cardQR", "cardPower")
SCREENSAVERS = ("screensaver", "screensaver2", "screensaver3")
MAX_GRID = 6  # meer rijen: het scherm schakelt naar de brede variant
TOGGLES = ("light", "switch", "input_boolean", "automation", "fan")
FORECAST_SENSORS = ("sensor.weather_forecast_daily", "sensor.weather_forecast_hourly")
BUTTONS = ("button", "input_button", "scene", "script")


class PanelError(ValueError):
    pass


class Env(Protocol):
    def state(self, entity: str) -> tuple[str, dict] | None: ...
    def template(self, text: str) -> Any: ...  # None zolang het antwoord van HA nog niet binnen is
    def forecast(self, entity: str) -> list[dict]: ...
    def now(self) -> datetime: ...
    def call(self, domain: str, service: str, data: dict) -> None: ...
    # Optioneel, voor rijen `kk.<...>` (kaarten uit de add-on, zie cards.py): hun stand en wat een druk doet
    # def dynamic(self, entity: str, lang: str) -> dict | None: ...   {kind, value, name, icon, color}
    # def press(self, entity: str, kind: str, value: str | None) -> None: ...


DYN = "kk"


# ---------------------------------------------------------------- teksten

DAYS = {"nl": ["ma", "di", "wo", "do", "vr", "za", "zo"], "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}
DAYS_LONG = {"nl": ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"],
             "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
MONTHS = {"nl": ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"],
          "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]}
WORDS = {"nl": {"press": "Druk", "run": "Start", "activate": "Activeer", "missing": "Niet gevonden", "check": "controleer de config"},
         "en": {"press": "Press", "run": "Run", "activate": "Activate", "missing": "Not found", "check": "check config"}}

WEATHER_ICON = {"clear-night": "weather-night", "cloudy": "weather-cloudy", "exceptional": "alert-circle-outline", "fog": "weather-fog",
                "hail": "weather-hail", "lightning": "weather-lightning", "lightning-rainy": "weather-lightning-rainy",
                "partlycloudy": "weather-partly-cloudy", "pouring": "weather-pouring", "rainy": "weather-rainy", "snowy": "weather-snowy",
                "snowy-rainy": "weather-snowy-rainy", "sunny": "weather-sunny", "windy": "weather-windy", "windy-variant": "weather-windy-variant"}
WEATHER_COLOR = {"partlycloudy": 38066, "windy": 38066, "clear-night": 38060, "cloudy": 31728, "exceptional": 63878, "fog": 38066,
                 "hail": 65535, "snowy": 65535, "lightning": 65120, "lightning-rainy": 50400, "pouring": 12703, "rainy": 25375,
                 "snowy-rainy": 38079, "sunny": 65504}
SENSOR_ICON = {"temperature": "thermometer", "power": "flash", "energy": "flash", "voltage": "flash", "current": "flash", "humidity": "water-percent",
               "battery": "battery", "pressure": "gauge", "signal_strength": "signal", "timestamp": "calendar-clock"}
DOMAIN_ICON = {"button": "gesture-tap-button", "input_button": "gesture-tap-button", "scene": "palette", "script": "script-text",
               "switch": "light-switch", "automation": "robot", "number": "ray-vertex", "input_number": "ray-vertex", "light": "lightbulb",
               "fan": "fan", "person": "account", "timer": "timer-outline", "navigate": "gesture-tap-button"}


def rgb565(rgb) -> int:
    r, g, b = (int(x) for x in rgb[:3])
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


ON_COLOR = rgb565([253, 216, 53])


def _scale(v: float, src: tuple[float, float], dst: tuple[float, float]) -> float:
    return (v - src[0]) / (src[1] - src[0]) * (dst[1] - dst[0]) + dst[0]


def _is_template(s: Any) -> bool:
    return isinstance(s, str) and ("{{" in s or "{%" in s)


def _walk(o: Any):
    if isinstance(o, dict):
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)
    else:
        yield o


# ---------------------------------------------------------------- configuratie

@dataclass
class Item:
    raw: dict
    entity: str
    name: str | None = None
    icon: Any = None
    color: Any = None
    status: str | None = None
    state: str | None = None
    state_not: str | None = None
    state_template: str | None = None
    type: Any = None
    value: str | None = None
    speed: Any = 0

    @classmethod
    def parse(cls, raw: Any) -> "Item":
        if not isinstance(raw, dict) or not isinstance(raw.get("entity"), str):
            raise PanelError(f"een entiteit verwacht met `entity:`, kreeg {raw!r}")
        g = raw.get
        return cls(raw, raw["entity"], g("name"), g("icon"), g("color"), g("status"), g("state"), g("state_not"), g("state_template"), g("type"), g("value"), g("speed", 0))


@dataclass
class Card:
    raw: dict
    type: str
    title: str
    key: str
    items: list[Item]
    hidden: bool = False
    uid: str = ""
    prev: str | None = None
    next: str | None = None
    nav: list[Item | None] = field(default_factory=lambda: [None, None])

    @classmethod
    def parse(cls, raw: Any, hidden: bool, n: int) -> "Card":
        if not isinstance(raw, dict):
            raise PanelError("een kaart moet een object zijn")
        typ = str(raw.get("type", "unknown"))
        title = str(raw.get("title", "unknown"))
        if typ not in CARD_TYPES and typ not in SCREENSAVERS:
            raise PanelError(f"kaart '{title}': het type {typ} wordt (nog) niet ondersteund; kies uit {', '.join(CARD_TYPES)}")
        c = cls(raw, typ, title, str(raw.get("key", "unknown")), [Item.parse(e) for e in raw.get("entities") or []], hidden, f"uuid.{'h' if hidden else 'c'}{n}")
        c.nav = [Item.parse(raw[k]) if raw.get(k) else None for k in ("navItem1", "navItem2")]
        return c


DEFAULTS = {"model": "eu", "sleepTimeout": 20, "sleepBrightness": 20, "screenBrightness": 100, "defaultBackgroundColor": "ha-dark",
            "featureExperimentalSliders": False, "locale": "en_US", "timeFormat": "%H:%M", "dateAdditionalTemplate": "", "timeAdditionalTemplate": ""}


def extract(source: Any) -> dict:
    """Een YAML-tekst (of al gelezen dict): de `config:` van een AppDaemon-app, of de configuratie zelf."""
    if isinstance(source, str):
        try:
            source = yaml.safe_load(source)
        except yaml.YAMLError as e:
            raise PanelError(f"YAML klopt niet: {e}") from e
    if not isinstance(source, dict):
        raise PanelError("verwacht een YAML-object met `cards:`")
    if "cards" not in source:
        apps = [v for v in source.values() if isinstance(v, dict) and isinstance(v.get("config"), dict)]
        if "config" in source and isinstance(source["config"], dict):
            source = source["config"]
        elif len(apps) == 1:
            source = apps[0]["config"]
        elif len(apps) > 1:
            raise PanelError("meerdere apps in de YAML: zet er één neer (één paneel per keer)")
    if not isinstance(source.get("cards"), list) or not source["cards"]:
        raise PanelError("geef minstens één kaart onder `cards:`")
    return source


class Panel:
    def __init__(self, source: Any, env: Env, send: Callable[[str], None], icons: dict[str, str] | None = None) -> None:
        cfg = extract(source)
        self.icons = {**ICONS, **(icons or {})}
        self.cfg = {**DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}
        self.env, self._tx = env, send
        self.lang = "nl" if str(self.cfg["locale"]).lower().startswith("nl") else "en"
        cards = [Card.parse(c, False, i) for i, c in enumerate(self.cfg["cards"])]
        ids = [c.uid for c in cards]
        if len(cards) > 1:
            for i, c in enumerate(cards):
                c.prev, c.next = ids[i - 1], ids[(i + 1) % len(ids)]
        self.cards = cards + [Card.parse(c, True, i) for i, c in enumerate(self.cfg.get("hiddenCards") or [])]
        sc = {"type": "screensaver", "key": "screensaver", "title": "screensaver", **(self.cfg.get("screensaver") or {})}
        self.saver = Card.parse({k: v for k, v in sc.items() if k != "entities"}, False, 0)
        self.saver.uid = "uuid.saver"
        self.saver.items = [Item.parse(e) for e in sc.get("entities") or []]
        if sc.get("inNavigation", True) and (self.saver.items or sc.get("entity")):  # de screensaver als stap tussen de laatste en de eerste kaart
            cards[0].prev = cards[-1].next = self.saver.uid
        self.by_uid = {c.uid: c for c in [*self.cards, self.saver]}
        self.current = self.saver
        self.history: list[Card] = [self.default_card()]
        self._awake_since: datetime | None = None
        self._last_page = ""
        self._last_saver: list[str] = []
        self._last_clock: tuple = ()
        self._seen_startup = False
        # rijen waarvan de stand uit de add-on komt: de hub ververst dan elke tik (alleen wat veranderd is gaat naar het scherm)
        self.dynamic = any(it.entity.startswith(f"{DYN}.") for c in self.cards for it in c.items)

    # ------------------------------------------------------------ wat volgen we

    def entities(self) -> set[str]:
        out: set[str] = set()
        for c in [*self.cards, self.saver]:
            for it in [*c.items, *c.nav]:
                if it:
                    out.add(it.entity)
                    if it.status:
                        out.add(it.status)
        for n in (1, 2):
            s = (self.cfg.get("screensaver") or {}).get(f"statusIcon{n}")
            if isinstance(s, dict) and s.get("entity"):
                out.add(s["entity"])
        sc = self.cfg.get("screensaver") or {}
        if not self.saver.items and sc.get("entity"):
            out.add(sc["entity"])
        for k in ("sleepBrightness", "screenBrightness"):
            if isinstance(self.cfg[k], str):
                out.add(self.cfg[k])
        return {e for e in out if "." in e and e.split(".")[0] not in ("navigate", "iText", "delete", "service", DYN)}

    def forecast_entities(self) -> set[str]:
        """Echte weer-entiteiten waar een voorspelling voor nodig is (een sensor met een `forecast`-attribuut heeft dat al)."""
        return {i.entity for c in [*self.cards, self.saver] for i in c.items if isinstance(i.type, (int, str)) and i.entity.startswith("weather.")}

    def templates(self) -> set[str]:
        return {s for s in _walk(self.cfg) if _is_template(s)}

    # ------------------------------------------------------------ hulpjes

    def default_card(self) -> Card:
        d = (self.cfg.get("screensaver") or {}).get("defaultCard")
        found = self.find(self.tpl(d)) if d else None
        return found or self.cards[0]

    def find(self, ident: Any) -> Card | None:
        ident = str(ident or "").removeprefix("navigate.")
        if ident in self.by_uid:
            return self.by_uid[ident]
        for c in self.cards:
            if c.key == ident or f"{c.type}_{c.key}" == ident:
                return c
        return self.saver if ident == self.saver.key else None

    def tpl(self, text: Any) -> Any:
        return self.env.template(text) if _is_template(text) else text

    def word(self, w: str) -> str:
        return WORDS[self.lang][w]

    def icon(self, name: Any) -> str:
        if not isinstance(name, str):
            return self.icons["alert-circle-outline"]
        return self.icons.get(name.removeprefix("mdi:"), self.icons["alert-circle-outline"])

    def state(self, entity: str | None) -> tuple[str, dict] | None:
        return self.env.state(entity) if entity and "." in entity else None

    # ------------------------------------------------------------ kleur en icoon

    def color(self, entity: str | None, st: tuple[str, dict] | None, override: Any = None, state_override: str | None = None) -> int:
        if override is not None:
            if isinstance(override, dict):
                s = st[0] if st else None
                for k, v in override.items():
                    if str(k) == s:
                        return self._rgb(v)
            else:
                return self._rgb(override)
        if st is None:
            return DEFAULT_COLOR
        state, attrs = (state_override or st[0]), st[1]
        domain = "weather" if entity in FORECAST_SENSORS else (entity or "").split(".")[0]
        color = ON_COLOR if state in ("on", "unlocked", "above_horizon", "home", "active") else DEFAULT_COLOR
        if domain == "weather":
            color = WEATHER_COLOR.get(state, color)
        rgb, bright = attrs.get("rgb_color"), attrs.get("brightness")
        if rgb:
            color = rgb565(self._bright(rgb, bright) if bright else rgb)
        elif bright:
            color = rgb565(self._bright([253, 216, 53], bright))
        return color

    def _rgb(self, v: Any) -> int:
        v = self.tpl(v)
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            return rgb565(v)
        return DEFAULT_COLOR  # een template zonder (geldig) antwoord

    @staticmethod
    def _bright(rgb, brightness) -> list[int]:
        b = int(_scale(float(brightness), (0, 255), (70, 255)))
        return [int(c / 255 * b) for c in rgb[:3]]

    def ha_icon(self, entity: str, st: tuple[str, dict] | None, override: Any = None, state_override: str | None = None) -> str:
        state = (state_override or (st[0] if st else "")) if st or state_override else ""
        if isinstance(override, str):
            return self.icon(override)
        if isinstance(override, dict):
            for k, v in override.items():
                if str(k) == state:
                    return self.icon(v)
        domain = entity.split(".")[0]
        attrs = st[1] if st else {}
        if entity in FORECAST_SENSORS:
            domain = "weather"
        if domain == "weather":
            name = WEATHER_ICON.get(state, "alert-circle-outline")
        elif domain == "input_boolean":
            name = "check-circle-outline" if state == "on" else "close-circle-outline"
        elif domain == "lock":
            name = "lock-open" if state == "unlocked" else "lock"
        elif domain == "sun":
            name = "weather-sunset-up" if state == "above_horizon" else "weather-sunset-down"
        elif domain == "sensor":
            name = SENSOR_ICON.get(attrs.get("device_class", ""), "alert-circle-outline")
        elif domain == "binary_sensor":
            name = "checkbox-marked-circle" if state == "on" else "radiobox-blank"
        else:
            name = DOMAIN_ICON.get(domain, "alert-circle-outline")
        return self.icon(name)

    # ------------------------------------------------------------ een rij op een pagina

    def item(self, it: Item, card_type: str, mask: bool = False) -> str:
        """`~soort~id~icoon~kleur~naam~waarde`, of "" als een voorwaarde de rij verbergt."""
        entity = it.entity
        domain = entity.split(".")[0] if "." in entity else "delete" if entity == "delete" else ""
        if entity in FORECAST_SENSORS:
            domain = "weather"  # een sensor met een voorspelling, zoals de template-sensor uit de weer-integratie
        name = self.tpl(it.name) if it.name is not None else None
        st = self.state(entity)
        status = self.state(it.status)
        cond = status[0] if status else st[0] if st else None
        if cond is not None and ((it.state is not None and it.state != cond) or (it.state_not is not None and it.state_not == cond)):
            return ""
        if it.state_template and self.tpl(it.state_template) is True:
            return ""
        if entity == "delete" or domain == "delete":
            return "~delete~~~~~"
        if domain == "navigate":
            target = self.find(entity)
            if target is None:
                return f"~text~{entity}~{self.icon('alert-circle-outline')}~{DEFAULT_COLOR}~page not found~"
            if status:
                icon, color = self.ha_icon(it.status or "", status, it.icon), self.color(it.status, status, it.color)
                if it.status.startswith("sensor") and card_type.startswith("cardGrid") and it.icon is None:
                    icon = self._short(status[0])
            else:
                icon, color = self.ha_icon(entity, None, it.icon), self.color(None, None, it.color)
            return f"~button~{entity}~{icon}~{color}~{name if name is not None else target.title}~{self.word('press')}"
        if domain == DYN:
            return self._dynamic(it, entity, name)
        if domain == "iText":
            value = entity.split(".", 1)[1] if entity.count(".") else ""  # alles na de eerste punt: een wachtwoord mag een punt hebben
            color = rgb565(it.color) if isinstance(it.color, list) else DEFAULT_COLOR
            return f"~text~{entity}~{self.icon(it.icon) if it.icon else self.icon('alert-circle-outline')}~{color}~{name if name is not None else 'conf name missing'}~{value}"
        if st is None:
            return f"~text~{entity}~{self.icon('alert-circle-outline')}~{DEFAULT_COLOR}~{self.word('missing')}~{self.word('check')}"
        state, attrs = st
        kind, value = "text", ""
        icon = self.ha_icon(entity, st, it.icon)
        color = self.color(entity, st, it.color)
        name = name if name is not None else attrs.get("friendly_name") or entity
        if domain in TOGGLES:
            kind, value = {"light": "light", "fan": "fan"}.get(domain, "switch"), 1 if state == "on" else 0
        elif domain in ("sensor", "binary_sensor"):
            dc, unit = attrs.get("device_class", ""), attrs.get("unit_of_measurement", "")
            value = state
            if domain == "sensor":
                try:
                    value = str(round(float(value), 1))
                except ValueError:
                    pass
            if dc != "temperature":
                value += " "
            value += unit
            if domain == "binary_sensor":
                value = state
            if domain == "sensor" and card_type.startswith("cardGrid") and it.icon is None:
                icon = self._short(state)
        elif domain in BUTTONS:
            kind, value = "button", self.word("activate" if domain == "scene" else "run" if domain == "script" else "press")
        elif domain in ("number", "input_number"):
            kind, value = "number", f"{state}|{attrs.get('min', 0)}|{attrs.get('max', 100)}"
        elif domain in ("input_select", "select"):
            kind, value = "input_sel", state
        elif domain == "weather":
            value, icon, color, name = self._weather(it, entity, st, icon, color, name)
        else:
            value = state
        if it.value is not None:  # sjabloon met een stuk tekst erachter: "{{ states('x') }} W"
            head, brace, tail = it.value.rpartition("}")
            rendered = self.env.template(head + brace) if brace else None
            value = f"{rendered if rendered is not None else ''}{tail}" if brace else it.value
        if mask:
            kind, entity = "", ""
        return f"~{kind}~{entity}~{icon}~{color}~{name}~{value}"

    def _dynamic(self, it: Item, entity: str, name: str | None) -> str:
        fn = getattr(self.env, "dynamic", None)
        d = fn(entity, self.lang) if fn else None
        if d is None:
            return f"~text~{entity}~{self.icon('alert-circle-outline')}~{DEFAULT_COLOR}~{name if name is not None else self.word('missing')}~"
        color = d.get("color", DEFAULT_COLOR)
        if it.color is not None:
            color = self._rgb(it.color)
        elif isinstance(color, (list, tuple)):
            color = rgb565(color)
        icon = self.icon(it.icon if it.icon is not None else d.get("icon") or "alert-circle-outline")
        shown = name if name is not None else d.get("name", "")
        return f"~{d.get('kind', 'text')}~{entity}~{icon}~{color}~{str(shown).replace('~', '-')}~{str(d.get('value', '')).replace('~', '-')}"

    @staticmethod
    def _short(state: str) -> str:
        """Een getal als pictogram: de eerste 4 tekens, zonder punt aan het eind."""
        s = state[:4]
        return s[:-1] if s.endswith(".") else s

    def _weather(self, it: Item, entity: str, st: tuple[str, dict], icon: str, color: int, name: str):
        attrs = st[1]
        unit = attrs.get("temperature_unit") or ""
        idx = it.type
        if isinstance(idx, str) and idx.isdigit():
            idx = int(idx)
        if isinstance(idx, int) and not isinstance(idx, bool):
            fc = attrs.get("forecast") or self.env.forecast(entity)
            if len(fc) > idx:
                day = fc[idx]
                try:
                    when = datetime.fromisoformat(str(day["datetime"]).replace("Z", "+00:00")).astimezone(self.env.now().tzinfo)
                    name = it.name if it.name is not None else DAYS[self.lang][when.weekday()]
                except (KeyError, ValueError):
                    pass
                cond = day.get("condition")
                icon = self.ha_icon(entity, st, None, cond)
                return f"{day.get('temperature', '')}{unit}", icon, self.color(entity, st, it.color, cond), name
        return f"{attrs.get('temperature', '')}{unit}", icon, color, name

    # ------------------------------------------------------------ pagina's

    def nav_buttons(self, card: Card) -> str:
        def nav(target: str | None, icon: str) -> str:
            if not target:
                return "delete~~~~~"
            return self.item(Item({}, f"navigate.{target}", icon=icon, color=[255, 255, 255]), "cardGrid")[1:]

        left = nav(card.prev, "mdi:arrow-left-bold")
        right = nav(card.next, "mdi:arrow-right-bold")
        if card.hidden:
            left, right = f"x~navUp~{self.icon('arrow-up-bold')}~{WHITE}~~", "delete~~~~~"
        if card.nav[0]:
            left = self.item(card.nav[0], "cardGrid")[1:]
        if card.nav[1]:
            right = self.item(card.nav[1], "cardGrid")[1:]
        return f"{left}~{right}"

    def page_type(self, card: Card) -> str:
        t = card.type
        if t == "cardGrid" and len(card.items) > MAX_GRID:
            return "cardGrid2"
        return "cardGrid" if t == "cardGrid1" else t

    def render(self, card: Card, page_type: bool = True) -> None:
        if page_type:
            self._tx(f"pageType~{self.page_type(card)}")
            self._last_page = ""
            timeout = card.raw.get("sleepTimeout", self.cfg["sleepTimeout"])
            self._tx(f"timeout~{timeout}")
        if card.type in SCREENSAVERS:
            self._saver_pages(force=page_type)
            return
        body = f"entityUpd~{card.title}~{self.nav_buttons(card)}"
        ct = self.page_type(card)
        if card.type == "cardQR":
            body += f"~{self.tpl(card.raw.get('qrCode', ''))}"
        for it in card.items:
            row = self.item(it, ct)
            body += row
            if card.type == "cardPower":
                speed = self.tpl(str(it.speed)) if self.state(it.entity) else None
                body += f"~{0 if speed is None else speed}"
        if body != self._last_page:
            self._last_page = body
            self._tx(body)

    def _saver_pages(self, force: bool) -> None:
        msgs = self._saver_messages()
        if force or msgs != self._last_saver:
            self._last_saver = msgs
            for m in msgs:
                self._tx(m)

    def _saver_messages(self) -> list[str]:
        out: list[str] = []
        sc = self.cfg.get("screensaver") or {}
        items = self.saver.items
        if not items and sc.get("entity"):
            items = [Item.parse({"entity": sc["entity"]}), *[Item.parse({"entity": sc["entity"], "type": i}) for i in range(4)]]
        out.append("weatherUpdate" + "".join(self.item(i, "cardEntities", mask=True) for i in items))
        theme = sc.get("theme")
        if isinstance(theme, dict):
            keys = ("background", "time", "timeAMPM", "date", "tMainText", "tForecast1", "tForecast2", "tForecast3", "tForecast4", "tForecast1Val",
                    "tForecast2Val", "tForecast3Val", "tForecast4Val", "bar", "tMainTextAlt2", "tTimeAdd")
            out.append("color" + "".join(f"~{rgb565(theme[k]) if k in theme else (0 if k == 'background' else WHITE)}" for k in keys))
        icons, alt = "", ""
        for n in (1, 2):
            s = sc.get(f"statusIcon{n}")
            st = self.state(s.get("entity")) if isinstance(s, dict) else None
            if st:
                icons += f"~{self.ha_icon(s['entity'], st, s.get('icon'))}~{self.color(s['entity'], st, s.get('color'))}"
                alt += f"~{s.get('altFont', '')}"
            else:
                icons += "~~"
                alt += "~"
        out.append(f"statusUpdate{icons}{alt}")
        return out

    # ------------------------------------------------------------ klok en helderheid

    def dim(self) -> str:
        sleep, bright = self._level(self.cfg["sleepBrightness"]), self._level(self.cfg["screenBrightness"])
        if self._awake_since is not None:  # eerste tik op de screensaver: helder, tot de slaaptijd om is
            sleep = bright
        if sleep == bright:  # dezelfde waarde breekt de slaaptimer van de firmware
            sleep -= 1
        bg = self.cfg["defaultBackgroundColor"]
        dbc = rgb565(bg) if isinstance(bg, list) else 6371 if bg == "ha-dark" else 0
        return f"dimmode~{sleep}~{bright}~{dbc}~~{1 if self.cfg['featureExperimentalSliders'] else 0}"

    def _level(self, v: Any) -> int:
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            st = self.state(v)
            try:
                return int(float(st[0])) if st else 20
            except ValueError:
                return 20
        if isinstance(v, list) and v:  # [{time: "22:00", value: 5}, ...]: de laatste waarde vóór nu
            now = self.env.now()
            minutes = now.hour * 60 + now.minute
            rows = sorted(((int(str(r["time"])[:2]) * 60 + int(str(r["time"])[3:5]), int(r["value"])) for r in v))
            before = [val for t, val in rows if t <= minutes]
            return before[-1] if before else rows[-1][1]
        return 20

    def date_text(self) -> str:
        n = self.env.now()
        fmt = self.cfg.get("dateFormat")
        if fmt:
            return n.strftime(fmt)
        d, m = DAYS_LONG[self.lang][n.weekday()], MONTHS[self.lang][n.month - 1]
        return f"{d} {n.day} {m} {n.year}" if self.lang == "nl" else f"{d}, {m} {n.day}, {n.year}"

    def clock(self, force: bool = False) -> None:
        now = self.env.now()
        tt, dt = self.cfg["timeAdditionalTemplate"], self.cfg.get("dateAdditionalTemplate", "")
        key = (now.strftime(self.cfg["timeFormat"]), now.date(), self.dim(), self.tpl(tt), self.tpl(dt))
        if key == self._last_clock and not force:
            return
        old = self._last_clock
        self._last_clock = key
        self._tx(f"time~{key[0]}~{key[3] or ''}")
        if force or not old or old[1] != key[1] or old[4] != key[4]:
            self._tx(f"date~{self.date_text()}{key[4] or ''}")
        if force or not old or old[2] != key[2]:
            self._tx(key[2])

    # ------------------------------------------------------------ levenscyclus

    def reset(self) -> None:
        """Het paneel (opnieuw) verbonden: terug naar het startscherm tot het zelf zegt dat het klaar is."""
        self._seen_startup = False
        self.current = self.saver
        self.history = [self.default_card()]
        self._tx("pageType~pageStartup")

    def startup(self) -> None:
        self._seen_startup = True
        self._awake_since = None
        self.current = self.saver
        self.history = [self.default_card()]
        self.clock(force=True)
        self.render(self.saver)

    def refresh(self, force: bool = False) -> None:
        """Een stand of template is veranderd: de huidige pagina bijwerken, zonder naar haar toe te springen.
        `force`: ook als er niets veranderd lijkt (een pakket kan verloren zijn)."""
        if self._seen_startup:
            if force:
                self._last_page, self._last_saver = "", []
            self.clock(force=force)
            self.render(self.current, page_type=False)

    def tick(self) -> None:
        if self._seen_startup:
            timeout = self.cfg["sleepTimeout"]
            if self._awake_since is not None and isinstance(timeout, int) and timeout > 0 and (self.env.now() - self._awake_since).total_seconds() >= timeout:
                self._awake_since = None  # niemand tikte verder: terug naar donker
            self.clock()

    # ------------------------------------------------------------ van het scherm

    def on_screen(self, text: str) -> None:
        msg = text.split(",")
        if len(msg) < 2 or msg[0] != "event":
            return
        kind = msg[1]
        if kind == "startup":
            self.startup()
        elif kind == "sleepReached":
            self._awake_since = None
            self.clock()
            self.history.append(self.current)
            self.current = self.saver
            self.render(self.saver)
        elif kind == "buttonPress2" and len(msg) >= 4:
            self.press(msg[2], msg[3], msg[4] if len(msg) > 4 else None)
        elif kind == "pageOpenDetail":
            log.info("detailpagina %s van %s wordt (nog) niet ondersteund", msg[2] if len(msg) > 2 else "?", msg[3] if len(msg) > 3 else "?")

    def press(self, entity: str, kind: str, value: str | None) -> None:
        domain = entity.split(".")[0]
        if entity == "screensaver" and kind == "bExit":
            d = (self.cfg.get("screensaver") or {}).get("defaultCard")
            found = self.find(self.tpl(d)) if d else None
            if found:
                self.history = [found]
            if not self.history:
                self.history.append(self.default_card())
            sc = self.cfg.get("screensaver") or {}
            taps = int(value or 0)
            if sc.get("doubleTapToUnlock") and taps < 2:
                return
            if sc.get("tapToWake", True) and self._awake_since is None and taps < 2:
                self._awake_since = self.env.now()  # eerste tik: de screensaver helder, de pagina's pas bij de volgende
                self.clock()
                return
            self._awake_since = None
            self.clock()
            self.current = self.history.pop()
            self.render(self.current)
            return
        if kind == "bExit":
            self.render(self.current)
            return
        if domain == DYN:
            fn = getattr(self.env, "press", None)
            if fn:
                fn(entity, kind, value)
            self.refresh()  # de nieuwe stand meteen tonen zodra de add-on hem kent
            return
        if kind == "OnOff":
            self.env.call("homeassistant", "turn_on" if value == "1" else "turn_off", {"entity_id": entity})
        elif kind == "number-set" and domain in ("number", "input_number"):
            self.env.call(domain, "set_value", {"entity_id": entity, "value": value})
        elif kind in ("up", "stop", "down") and domain == "cover":
            self.env.call("cover", {"up": "open_cover", "stop": "stop_cover", "down": "close_cover"}[kind], {"entity_id": entity})
        elif kind == "button":
            self._button(entity, domain)
        elif kind not in ("bExit",):
            log.info("knop %s van %s wordt (nog) niet ondersteund", kind, entity)

    def _to_saver(self) -> None:
        """Met een pijl naar de screensaver: helder, want je wilt hem zien. Hij heeft zelf geen pijlen: een tik gaat verder in dezelfde richting."""
        top = [c for c in self.cards if not c.hidden]
        here = self.current
        after = top[0] if here is top[-1] and here.next == self.saver.uid else top[-1] if here is top[0] and here.prev == self.saver.uid else here
        self.history = [after]
        self._awake_since = self.env.now()
        self.clock()
        self.current = self.saver
        self.render(self.saver)

    def _button(self, entity: str, domain: str) -> None:
        if domain == "navigate":
            target = self.find(entity)
            if target is None:
                log.warning("pagina %s bestaat niet", entity)
                return
            if target is self.saver:
                self._to_saver()
                return
            if target.hidden:
                self.history.append(self.current)
            self.current = target
            self.render(target)
        elif entity.startswith("navUp"):
            self.current = self.history.pop() if self.history else self.default_card()
            self.render(self.current)
        elif domain in ("scene", "script"):
            self.env.call(domain, "turn_on", {"entity_id": entity})
        elif domain in TOGGLES:
            self.env.call(domain, "toggle", {"entity_id": entity})
        elif domain in ("button", "input_button"):
            self.env.call(domain, "press", {"entity_id": entity})
        elif domain == "lock":
            st = self.state(entity)
            self.env.call("lock", "unlock" if st and st[0] == "locked" else "lock", {"entity_id": entity})


class _NoEnv:
    def state(self, entity):
        return None

    def template(self, text):
        return None

    def forecast(self, entity):
        return []

    def now(self):
        return datetime.now()

    def call(self, domain, service, data):
        pass


def validate(source: Any) -> None:
    """Gooit PanelError als deze configuratie niet te gebruiken is."""
    Panel(source, _NoEnv(), lambda _text: None)
