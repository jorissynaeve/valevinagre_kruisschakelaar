"""Een scherm met openHASP-firmware: de kaarten uit het model worden JSONL voor het scherm, en een aanraking een bevel.

Pure logica, zonder I/O: `Screen` krijgt een `Env` (standen, bevelen) en een `send(commando, inhoud)`; de hub (hasphub.py) doet MQTT.
Het protocol is dat van de firmware (zie DESIGN.md §27):

  naar het scherm   hasp/<scherm>/command/jsonl         objecten maken
                    hasp/<scherm>/command/p1b2.bg_color kleur van een knop
                    hasp/<scherm>/command/p1b2.text     tekst van een knop
                    hasp/<scherm>/command/page          pagina kiezen
  van het scherm    hasp/<scherm>/LWT                   online / offline (retained, de broker meldt een weggevallen scherm)
                    hasp/<scherm>/state/p1b2            {"event":"down"}
                    hasp/<scherm>/state/backlight       {"state":"on","brightness":255}

Een scherm toont dezelfde kaarten als een NSPanel (cards.py), in de vorm van `cards._card`: een titel en rijen met een entiteit.
Zo'n rij is een HA-entiteit, `kk.<kaart>__<deel>` (een lamp, waarde of laadpaal uit de add-on, via `Env.dynamic`/`Env.press`),
`navigate.<kaart>` (een subpagina), `iText.<tekst>` (alleen tekst) of `delete` (een lege plek). Vier knoppen per pagina: een kaart
met meer rijen loopt door op een volgende pagina.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Protocol

log = logging.getLogger("kk.hasp")

LAYOUT_VERSION = 2  # ophogen als de indeling hieronder verandert: de schermen krijgen dan vanzelf het nieuwe ontwerp
MARKER_PAGE, MARKER_ID = 0, 250
MARKER = f"p{MARKER_PAGE}b{MARKER_ID}"
MAX_BUTTONS = 4
MAX_PAGES = 12  # zoveel pagina's heeft de firmware
MAX_CHUNK = 2000  # bytes per MQTT-bericht, zoals de bestaande koppeling op deze schermen stuurde
WAKE_GUARD = 0.7  # s: zo lang na het wakker worden telt een aanraking nog als "wakker maken"
DYN = "kk"  # zie cards.DYN

SLOTS = ((20, 110), (260, 110), (20, 250), (260, 250))
BTN_W, BTN_H = 200, 120
FIRST_BTN, NAV_PREV, NAV_MID, NAV_NEXT = 2, 6, 7, 8
ICON_LIGHT, ICON_PREV, ICON_HOME, ICON_NEXT = "", "", "", ""
C_PAGE, C_HEAD, C_NAV, C_TEXT, C_WHITE = "#FFFFFF", "#46B2ED", "#34495E", "#34495E", "#FFFFFF"
C_ON, C_OFF, C_UNKNOWN = "#FFC107", "#DDE3E8", "#9E9E9E"
PANEL_DEFAULT = 17299  # panel.DEFAULT_COLOR: de gewone kleur op een NSPanel, hier C_OFF
BACK = {"nl": "Terug", "en": "Back"}

ACTIONS = {"script": ("script", "turn_on"), "scene": ("scene", "turn_on"), "button": ("button", "press"), "input_button": ("input_button", "press")}
TOGGLES = ("switch", "light", "input_boolean", "fan", "automation")
DOMAINS = (*TOGGLES, *ACTIONS)
PRESSABLE = (*TOGGLES, "cover")  # wat een tik omschakelt; een sensor en dergelijke toont alleen zijn waarde


class LayoutError(ValueError):
    """De kaarten passen niet op het scherm."""


class Env(Protocol):
    def state(self, entity: str) -> tuple[str, dict] | None: ...
    def call(self, domain: str, service: str, data: dict) -> None: ...
    # Optioneel, voor rijen `kk.<...>`: zoals bij panel.Env
    # def dynamic(self, entity: str, lang: str) -> dict | None: ...   {kind, value, name, color, known}
    # def press(self, entity: str, kind: str, value: str | None) -> None: ...


def design_hash(design: Any) -> str:
    """Wat er op het scherm staat, zonder de standen: zo weten we na een herstart of het nog klopt."""
    text = json.dumps({"v": LAYOUT_VERSION, "design": design}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def chunks(lines: list[str], limit: int = MAX_CHUNK) -> list[str]:
    """Regels bundelen tot berichten van hoogstens `limit` bytes, nooit midden in een regel."""
    out: list[str] = []
    cur = ""
    for line in lines:
        if cur and len((cur + "\n" + line).encode("utf-8")) > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def hex565(v: int) -> str:
    """Een kleur van het NSPanel (RGB565) als #RRGGBB."""
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return f"#{r * 255 // 31:02X}{g * 255 // 63:02X}{b * 255 // 31:02X}"


def expand(cards: list[dict]) -> list[dict]:
    """Van kaarten naar de pagina's van de firmware: [{title, tiles, group, sub}], een tegel {entity, name, nav}. Gooit LayoutError."""
    pages: list[dict] = []
    first: dict[str, int] = {}
    for gi, c in enumerate(cards):
        ents = [e for e in c.get("entities") or []]
        while ents and ents[-1].get("entity") == "delete":  # lege plekken achteraan maken geen pagina
            ents.pop()
        parts = [ents[i:i + MAX_BUTTONS] for i in range(0, len(ents), MAX_BUTTONS)] or [[]]
        first.setdefault(c.get("key") or "", len(pages) + 1)
        for k, part in enumerate(parts, 1):
            title = c.get("title") or ""
            pages.append({"title": f"{title} ({k}/{len(parts)})" if len(parts) > 1 else title, "group": gi, "sub": bool(c.get("sub")), "tiles": part})
    if len(pages) > MAX_PAGES:
        raise LayoutError(f"Deze kaarten hebben {len(pages)} pagina's nodig; het scherm heeft er {MAX_PAGES}.")
    for p in pages:
        tiles = []
        for e in p["tiles"]:
            ent = e.get("entity") or "delete"
            if ent == "delete":
                tiles.append(None)
            elif ent.startswith("navigate."):
                target = first.get(ent.removeprefix("navigate."))
                tiles.append({"entity": None, "name": e.get("name"), "nav": target} if target else None)
            else:
                tiles.append({"entity": ent, "name": e.get("name"), "nav": None})
        p["tiles"] = tiles
    return pages


class Screen:
    def __init__(self, cards: list[dict], timeout: int, env: Env, send: Callable[[str, str], None], lang: str = "nl") -> None:
        """Gooit LayoutError als de kaarten niet passen."""
        if not cards:
            raise LayoutError("Hang minstens één kaart aan dit scherm.")
        self.timeout, self.env, self._tx, self.lang = timeout, env, send, lang
        self.pages = expand(cards)
        self.dark = False
        self.woke_at = -1e9
        self.last_touch = 0.0
        self.current = 1
        self._tiles: dict[tuple[int, int], dict] = {}  # (pagina, id) -> tegel
        self._nav: dict[tuple[int, int], int | str] = {}  # (pagina, id) -> pagina om naartoe te gaan, of "back"
        self._back: dict[int, int] = {}  # groep van een subpagina -> de pagina van waar je kwam
        self._shown: dict[tuple[int, int], tuple[str, str]] = {}  # (pagina, id) -> (tekst, kleur) zoals verstuurd
        main = [n for n, p in enumerate(self.pages, 1) if not p["sub"]]
        for n, page in enumerate(self.pages, 1):
            for i, t in enumerate(page["tiles"]):
                if t:
                    self._tiles[(n, FIRST_BTN + i)] = t
            ring = main if not page["sub"] else [m for m, p in enumerate(self.pages, 1) if p["group"] == page["group"]]
            if len(ring) > 1:
                at = ring.index(n)
                self._nav[(n, NAV_PREV)] = ring[at - 1]
                self._nav[(n, NAV_NEXT)] = ring[(at + 1) % len(ring)]
            if page["sub"]:
                self._nav[(n, NAV_MID)] = "back"
            elif len(ring) > 1:
                self._nav[(n, NAV_MID)] = ring[0]
        self.hash = design_hash({"lang": lang, "pages": self.pages})

    # ------------------------------------------------------------ wat volgen we

    def entities(self) -> set[str]:
        """De HA-entiteiten op het scherm (niet de rijen uit de add-on)."""
        return {t["entity"] for t in self._tiles.values() if t["entity"] and not t["entity"].startswith((f"{DYN}.", "iText."))}

    def color(self, entity: str | None) -> str:
        st = self.env.state(entity) if entity else None
        if st is None or st[0] in ("unavailable", "unknown"):
            return C_UNKNOWN
        return C_ON if st[0] == "on" else C_OFF

    def _dynamic(self, entity: str) -> dict | None:
        fn = getattr(self.env, "dynamic", None)
        return fn(entity, self.lang) if fn else None

    def look(self, t: dict) -> tuple[str, str]:
        """(tekst, kleur) van een tegel, met de stand van nu."""
        name, ent = t.get("name"), t["entity"]
        if t["nav"]:
            return f"{name or self.pages[t['nav'] - 1]['title']} {ICON_NEXT}", C_OFF
        if ent.startswith("iText."):
            return f"{name or ''}\n{ent.split('.', 1)[1]}".strip(), C_OFF
        if ent.startswith(f"{DYN}."):
            d = self._dynamic(ent)
            if d is None:
                return name or "?", C_UNKNOWN
            label = d.get("name") or name or ""
            if d.get("kind") == "switch":
                color = C_ON if d.get("value") == 1 else C_OFF if d.get("known", True) else C_UNKNOWN
                return f"{ICON_LIGHT} {label}", color
            c = d.get("color")
            color = hex565(c) if isinstance(c, int) and c != PANEL_DEFAULT else C_OFF
            value = str(d.get("text") if d.get("text") is not None else d.get("value") if d.get("value") is not None else "")  # `text`: voor een regelaar (kind number) die hier alleen een tekst is
            return f"{label}\n{value}".strip(), color
        st = self.env.state(ent)
        label = name or (st[1].get("friendly_name") if st and isinstance(st[1], dict) else None) or ent.split(".")[-1]
        domain = ent.split(".")[0]
        if domain in PRESSABLE or domain in ACTIONS:
            return f"{ICON_LIGHT} {label}", self.color(ent)
        if st is None or st[0] in ("unavailable", "unknown"):
            return label, C_UNKNOWN
        unit = st[1].get("unit_of_measurement") if isinstance(st[1], dict) else None
        return f"{label}\n{st[0]}{f' {unit}' if unit else ''}", C_OFF

    # ------------------------------------------------------------ het ontwerp

    def design(self) -> list[str]:
        """De regels voor `jsonl`, met de huidige standen. Het merkteken komt als allerlaatste: het zegt dat alles is aangekomen."""
        lines: list[dict[str, Any]] = []
        for n, page in enumerate(self.pages, 1):
            lines.append({"page": n, "id": 0, "bg_color": C_PAGE})
            lines.append({"page": n, "id": 1, "obj": "btn", "x": 0, "y": 0, "w": 480, "h": 40, "text": page["title"], "bg_color": C_HEAD, "bg_grad_dir": 0,
                          "text_color": C_WHITE, "radius": 0, "border_width": 0})
            for i in range(len(page["tiles"])):
                t = self._tiles.get((n, FIRST_BTN + i))
                if t is None:
                    continue
                x, y = SLOTS[i]
                text, color = self._shown[(n, FIRST_BTN + i)] = self.look(t)
                lines.append({"page": n, "id": FIRST_BTN + i, "obj": "btn", "x": x, "y": y, "w": BTN_W, "h": BTN_H, "text": text, "bg_color": color,
                              "bg_grad_dir": 0, "text_color": C_TEXT})
            for oid, x, w, text in ((NAV_PREV, 0, 160, ICON_PREV), (NAV_MID, 160, 160, ICON_HOME), (NAV_NEXT, 320, 160, ICON_NEXT)):
                to = self._nav.get((n, oid))
                if to is not None:
                    lines.append({"page": n, "id": oid, "obj": "btn", "x": x, "y": 450, "w": w, "h": 30, "text": BACK[self.lang] if to == "back" else text, "radius": 0,
                                  "bg_color": C_NAV, "bg_grad_dir": 0, "text_color": C_WHITE})
        lines.append({"page": MARKER_PAGE, "id": MARKER_ID, "obj": "label", "x": 0, "y": 0, "w": 1, "h": 1, "text": self.hash, "hidden": 1})
        return [json.dumps(x) for x in lines]

    # ------------------------------------------------------------ standen naar het scherm

    def sync(self, force: bool = False) -> None:
        """Tekst en kleur van elke tegel; alleen wat veranderd is gaat naar het scherm (met `force` alles)."""
        for key, t in self._tiles.items():
            text, color = self.look(t)
            old = self._shown.get(key)
            if force or old is None or old[0] != text:
                self._tx(f"p{key[0]}b{key[1]}.text", text)
            if force or old is None or old[1] != color:
                self._tx(f"p{key[0]}b{key[1]}.bg_color", color)
            self._shown[key] = (text, color)

    def ready(self, now: float) -> None:
        """Het ontwerp staat op het scherm: de tijd tot het donker worden begint nu."""
        self.last_touch = now
        self.current = 1
        self.sync(force=True)

    # ------------------------------------------------------------ licht van het scherm

    def tick(self, now: float) -> None:
        if self.timeout and not self.dark and now - self.last_touch >= self.timeout:
            self.dark = True
            self._tx("", "backlight off")

    def on_backlight(self, payload: str, now: float) -> None:
        data = _json(payload)
        if data.get("state") == "off":
            self.dark = True
        elif data.get("state") == "on" and self.dark:  # de firmware maakt het scherm zelf wakker bij een aanraking
            self.dark = False
            self.woke_at = self.last_touch = now

    # ------------------------------------------------------------ van het scherm

    def on_event(self, obj: str, payload: str, now: float) -> None:
        event = _json(payload).get("event")
        if event is None:
            return
        try:
            page, oid = (int(x) for x in obj[1:].split("b"))
        except ValueError:
            return
        if event != "down":
            self.last_touch = now
            return
        if self.dark or now - self.woke_at < WAKE_GUARD:  # wie op een donker scherm tikt, wil geen lamp schakelen maar iets zien
            if self.dark:
                self.dark = False
                self._tx("", "backlight on")
            self.woke_at = -1e9
            self.last_touch = now
            return
        self.last_touch = now
        to = self._nav.get((page, oid))
        if to == "back":
            self._go(self._back.get(self.pages[page - 1]["group"], 1))
        elif to is not None:
            self._go(to, page)
        elif (page, oid) in self._tiles:
            t = self._tiles[(page, oid)]
            if t["nav"]:
                self._go(t["nav"], page)
            else:
                self._press(t["entity"])

    def _go(self, to: int, came_from: int | None = None) -> None:
        if came_from is not None:
            g = self.pages[to - 1]["group"]
            if self.pages[to - 1]["sub"] and self.pages[came_from - 1]["group"] != g:
                self._back[g] = came_from  # terug brengt je waar je vandaan kwam
        self.current = to
        self._tx("page", str(to))

    def _press(self, entity: str) -> None:
        if entity.startswith("iText."):
            return
        if entity.startswith(f"{DYN}."):
            fn = getattr(self.env, "press", None)
            if fn:
                fn(entity, "button", None)  # een lamp schakelt om, de laadpaal start of stopt
            return
        domain = entity.split(".")[0]
        if domain in ACTIONS:
            self.env.call(*ACTIONS[domain], {"entity_id": entity})
        elif domain in PRESSABLE:
            self.env.call(domain, "toggle", {"entity_id": entity})


def _json(payload: str) -> dict:
    try:
        data = json.loads(payload)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
