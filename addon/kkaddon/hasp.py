"""Een scherm met openHASP-firmware: de configuratie uit het model wordt JSONL voor het scherm, en een aanraking een HA-bevel.

Pure logica, zonder I/O: `Screen` krijgt een `Env` (standen, bevelen) en een `send(commando, inhoud)`; de hub (hasphub.py) doet MQTT.
Het protocol is dat van de firmware (zie DESIGN.md §27):

  naar het scherm   hasp/<scherm>/command/jsonl         objecten maken
                    hasp/<scherm>/command/p1b2.bg_color kleur van een knop
                    hasp/<scherm>/command/page          pagina kiezen
  van het scherm    hasp/<scherm>/LWT                   online / offline (retained, de broker meldt een weggevallen scherm)
                    hasp/<scherm>/state/p1b2            {"event":"down"}
                    hasp/<scherm>/state/backlight       {"state":"on","brightness":255}
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Protocol

log = logging.getLogger("kk.hasp")

LAYOUT_VERSION = 1  # ophogen als de indeling hieronder verandert: de schermen krijgen dan vanzelf het nieuwe ontwerp
MARKER_PAGE, MARKER_ID = 0, 250
MARKER = f"p{MARKER_PAGE}b{MARKER_ID}"
MAX_BUTTONS = 4
MAX_PAGES = 12  # zoveel pagina's heeft de firmware
MAX_CHUNK = 2000  # bytes per MQTT-bericht, zoals de bestaande koppeling op deze schermen stuurde
WAKE_GUARD = 0.7  # s: zo lang na het wakker worden telt een aanraking nog als "wakker maken"

SLOTS = ((20, 110), (260, 110), (20, 250), (260, 250))
BTN_W, BTN_H = 200, 120
ICON_LIGHT, ICON_PREV, ICON_HOME, ICON_NEXT = "", "", "", ""
C_PAGE, C_HEAD, C_NAV, C_TEXT, C_WHITE = "#FFFFFF", "#46B2ED", "#34495E", "#34495E", "#FFFFFF"
C_ON, C_OFF, C_UNKNOWN = "#FFC107", "#DDE3E8", "#9E9E9E"

ACTIONS = {"script": ("script", "turn_on"), "scene": ("scene", "turn_on"), "button": ("button", "press"), "input_button": ("input_button", "press")}
TOGGLES = ("switch", "light", "input_boolean", "fan", "automation")
DOMAINS = (*TOGGLES, *ACTIONS)


class Env(Protocol):
    def state(self, entity: str) -> tuple[str, dict] | None: ...
    def call(self, domain: str, service: str, data: dict) -> None: ...


def design_hash(pages: list[dict]) -> str:
    """Wat er op het scherm staat, zonder de standen: zo weten we na een herstart of het nog klopt."""
    text = json.dumps({"v": LAYOUT_VERSION, "pages": pages}, sort_keys=True, ensure_ascii=False)
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


class Screen:
    def __init__(self, pages: list[dict], timeout: int, env: Env, send: Callable[[str, str], None]) -> None:
        self.pages, self.timeout, self.env, self._tx = pages, timeout, env, send
        self.hash = design_hash(pages)
        self.dark = False
        self.woke_at = -1e9
        self.last_touch = 0.0
        self._buttons: dict[tuple[int, int], str | None] = {}  # (pagina, id) -> entiteit
        self._nav: dict[tuple[int, int], int] = {}  # (pagina, id) -> pagina om naartoe te gaan
        self._colors: dict[tuple[int, int], str] = {}
        n = len(pages)
        for p, page in enumerate(pages, 1):
            for i, b in enumerate(page["buttons"]):
                self._buttons[(p, 2 + i)] = b.get("entity")
            if n > 1:
                self._nav[(p, 6)] = n if p == 1 else p - 1
                self._nav[(p, 7)] = 1
                self._nav[(p, 8)] = 1 if p == n else p + 1

    # ------------------------------------------------------------ wat volgen we

    def entities(self) -> set[str]:
        return {e for e in self._buttons.values() if e}

    def color(self, entity: str | None) -> str:
        st = self.env.state(entity) if entity else None
        if st is None or st[0] in ("unavailable", "unknown"):
            return C_UNKNOWN
        return C_ON if st[0] == "on" else C_OFF

    # ------------------------------------------------------------ het ontwerp

    def design(self) -> list[str]:
        """De regels voor `jsonl`, met de huidige standen. Het merkteken komt als allerlaatste: het zegt dat alles is aangekomen."""
        lines: list[dict[str, Any]] = []
        for p, page in enumerate(self.pages, 1):
            lines.append({"page": p, "id": 0, "bg_color": C_PAGE})
            lines.append({"page": p, "id": 1, "obj": "btn", "x": 0, "y": 0, "w": 480, "h": 40, "text": page["title"], "bg_color": C_HEAD, "bg_grad_dir": 0,
                          "text_color": C_WHITE, "radius": 0, "border_width": 0})
            for i, b in enumerate(page["buttons"]):
                x, y = SLOTS[i]
                color = self._colors[(p, 2 + i)] = self.color(b.get("entity"))
                lines.append({"page": p, "id": 2 + i, "obj": "btn", "x": x, "y": y, "w": BTN_W, "h": BTN_H, "text": f"{ICON_LIGHT} {self.label(b)}", "bg_color": color,
                              "bg_grad_dir": 0, "text_color": C_TEXT})
            if len(self.pages) > 1:
                for oid, x, w, text in ((6, 0, 160, ICON_PREV), (7, 160, 160, ICON_HOME), (8, 320, 160, ICON_NEXT)):
                    lines.append({"page": p, "id": oid, "obj": "btn", "x": x, "y": 450, "w": w, "h": 30, "text": text, "radius": 0, "bg_color": C_NAV, "bg_grad_dir": 0, "text_color": C_WHITE})
        lines.append({"page": MARKER_PAGE, "id": MARKER_ID, "obj": "label", "x": 0, "y": 0, "w": 1, "h": 1, "text": self.hash, "hidden": 1})
        return [json.dumps(x) for x in lines]

    @staticmethod
    def label(b: dict) -> str:
        return b.get("label") or (b.get("entity") or "?").split(".")[-1]

    # ------------------------------------------------------------ standen naar het scherm

    def sync(self, force: bool = False) -> None:
        for key, entity in self._buttons.items():
            color = self.color(entity)
            if force or self._colors.get(key) != color:
                self._colors[key] = color
                self._tx(f"p{key[0]}b{key[1]}.bg_color", color)

    def ready(self, now: float) -> None:
        """Het ontwerp staat op het scherm: de tijd tot het donker worden begint nu."""
        self.last_touch = now
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
        if (page, oid) in self._nav:
            self._tx("page", str(self._nav[(page, oid)]))
        elif self._buttons.get((page, oid)):
            self._press(self._buttons[(page, oid)])

    def _press(self, entity: str) -> None:
        domain = entity.split(".")[0]
        if domain in ACTIONS:
            self.env.call(*ACTIONS[domain], {"entity_id": entity})
        else:
            self.env.call(domain, "toggle", {"entity_id": entity})


def _json(payload: str) -> dict:
    try:
        data = json.loads(payload)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
