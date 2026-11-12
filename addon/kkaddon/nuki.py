"""Nuki-sloten via de Nuki Web API (api.nuki.io, een token) als HUISDEV-apparaat: een slot is een blok `e:<id>`.

Het pure deel staat hier: de lijst van sloten lezen, er per slot een descriptor van maken zoals HUISDEV §5 hem beschrijft, de waarden eruit halen en een
opdracht omzetten naar het verzoek van Nuki. Het netwerk (pollen, opdrachten) en de opslag zitten in espdevhub.py, zoals bij Hue.

  punten    stand (de code van Nuki, met namen), vergrendeld (0/1, onbekend zolang het slot beweegt), deur open (0/1, alleen met een deursensor),
            batterij (%), batterij kritiek (0/1)
  acties    vergrendelen (gewoon), ontgrendelen, deur vrijgeven (unlatch), lock 'n' go en lock 'n' go met unlatch: **gevaarlijk**

Een slot is een deur. Wat hem opent (`unlock`, `unlatch`, `lock 'n' go`) is daarom een actie met bevestiging, alleen met de hand uit de zijbalk: het is
geen punt dat een timer of module kan sturen, het staat niet als knop in HA en een MQTT-opdracht ervoor wordt geweigerd (HUISDEV `danger`, §34).

Nuki geeft geen gebeurtenissen zonder een adres dat vanaf het internet bereikbaar is, dus we pollen (de limiet is 4 per seconde, 100 per minuut en 1000
per uur per token, en de token wordt misschien ook door iets anders gebruikt). Een opdracht is asynchroon: een 200 of 204 betekent dat Nuki hem
aannam, niet dat het slot hem uitvoerde. De stand komt uit de volgende poll, die na een opdracht snel volgt.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import aiohttp

BASE = "https://api.nuki.io"
TIMEOUT = 10  # s per verzoek
MAX_BODY = 4 << 20

# Nuki's "state" (concepts-pagina van docs.nuki.io). Een slot kent er nog meer (8 tot 252 staan nergens beschreven): die zijn onbekend, nooit een gok.
STATES = {0: "Niet gekalibreerd", 1: "Vergrendeld", 2: "Ontgrendelen", 3: "Ontgrendeld", 4: "Vergrendelen", 5: "Vrijgegeven (unlatched)", 6: "Ontgrendeld (lock 'n' go)",
          7: "Vrijgeven (unlatching)", 253: "Opstarten", 254: "Motor geblokkeerd", 255: "Onbekend"}
LOCKED, OPEN = (1,), (3, 5, 6)  # de standen die we vergrendeld en niet vergrendeld noemen; de overgangen (2, 4, 7) en de rest zijn onbekend
DOOR_OPEN, DOOR_CLOSED = 3, 2  # doorState; 0 is "geen deursensor", 1 uitgeschakeld, 4 onbekend, 5 kalibreren

# actie -> (code van Nuki, label, gevaarlijk)
ACTIONS = {"lock": (2, "Vergrendelen", False), "unlock": (1, "Ontgrendelen", True), "unlatch": (3, "Deur vrijgeven (unlatch)", True),
           "lockngo": (4, "Lock 'n' go", True), "unlatch_lockngo": (5, "Unlatch met lock 'n' go", True)}


class NukiError(Exception):
    """Een fout bij Nuki, met een zin die zegt wat er mis is. `code` is `key` (token geweigerd), `rate` (te veel verzoeken) of `net`."""

    def __init__(self, code: str, reason: str, status: int = 0) -> None:
        super().__init__(reason)
        self.code, self.reason, self.status = code, reason, status


def lid_of(lock: dict) -> str | None:
    i = lock.get("smartlockId")
    return str(i) if isinstance(i, int) and not isinstance(i, bool) and i > 0 else None


def locks(data: Any) -> dict[str, dict]:
    """`GET /smartlock` als {id: slot}. Alles wat geen slot met een id is, vervalt."""
    return {lid_of(x): x for x in (data if isinstance(data, list) else []) if isinstance(x, dict) and lid_of(x)}


def hid_of(lid: str) -> str:
    return f"nuki-{lid}"[:32]


def account_id(token: str) -> str:
    """Een kort id voor de token, zonder dat het de token weggeeft (een hash van zes tekens)."""
    return "n_" + hashlib.sha256(token.encode()).hexdigest()[:6]


def _name(lock: dict) -> str:
    n = lock.get("name")
    return n.strip()[:60] if isinstance(n, str) and n.strip() else "Nuki-slot"


def listing(found: dict[str, dict]) -> list[dict]:
    """Wat Nuki aanbiedt, voor het menu: [{lid, hid, name, model}] op naam."""
    out = [{"lid": lid, "hid": hid_of(lid), "name": _name(x), "type": x.get("type") if isinstance(x.get("type"), int) else None} for lid, x in found.items()]
    return sorted(out, key=lambda x: (x["name"].lower(), x["lid"]))


# ------------------------------------------------------------ van slot naar punten en acties

def _pt(pid: str, label: str, kind: str, **kw) -> dict:
    return {"id": pid, "label": label, "kind": kind, "unit": "", "decimals": 0, "min": None, "max": None, "step": None, "options": [], "writable": False,
            "danger": False, "diag": False, "group": "", "cls": "", "state": "", "stale": 0, "confirm": "", **kw}


def _state(lock: dict) -> dict:
    s = lock.get("state")
    return s if isinstance(s, dict) else {}


def has_door_sensor(lock: dict) -> bool:
    d = _state(lock).get("doorState")
    return isinstance(d, int) and not isinstance(d, bool) and d != 0


def describe(lock: dict, name: str = "") -> dict:
    """De descriptor van een slot in de vorm van `espdevhub.clean_descriptor` (zonder device-velden). Een deursensor, een batterijpercentage en een
    kritieke batterij zijn punten als het slot ze meldt: een ouder slot zonder percentage krijgt dat punt niet."""
    st = _state(lock)
    pts = [_pt("state", "Stand", "enum", options=[{"v": k, "l": v} for k, v in STATES.items()]), _pt("locked", "Vergrendeld", "bool")]
    if has_door_sensor(lock):
        pts.append(_pt("door_open", "Deur open", "bool"))
    if isinstance(st.get("batteryCharge"), int) and not isinstance(st.get("batteryCharge"), bool):
        pts.append(_pt("battery", "Batterij", "int", unit="%", cls="battery", state="measurement", min=0, max=100, step=1))
    if isinstance(st.get("batteryCritical"), bool):
        pts.append(_pt("battery_low", "Batterij kritiek", "bool"))
    actions = [{"id": aid, "label": label, "danger": danger, "args": [],
                "confirm": f"{label}: het slot voert dit meteen uit en de deur kan daarna open. Doorgaan?" if danger else ""} for aid, (_, label, danger) in ACTIONS.items()]
    body = json.dumps([pts, actions], sort_keys=True, separators=(",", ":"))
    return {"proto": "1.0", "rev": hashlib.sha1(body.encode()).hexdigest()[:8], "id": hid_of(lid_of(lock) or "0"), "type": "Nuki-slot", "name": name or _name(lock), "fw": "", "lib": "",
            "points": pts, "actions": actions}


def values(lock: dict) -> dict[str, Any]:
    """De waarde per punt, of None (onbekend). Een stand die we niet kennen is onbekend, en `locked` ook zolang het slot beweegt."""
    st = _state(lock)
    s = st.get("state")
    known = isinstance(s, int) and not isinstance(s, bool) and s in STATES
    out: dict[str, Any] = {"state": s if known else None, "locked": True if known and s in LOCKED else False if known and s in OPEN else None}
    d = st.get("doorState")
    out["door_open"] = True if d == DOOR_OPEN else False if d == DOOR_CLOSED else None
    b = st.get("batteryCharge")
    out["battery"] = b if isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 100 else None
    c = st.get("batteryCritical")
    out["battery_low"] = c if isinstance(c, bool) else None
    return out


def action_request(lid: str, aid: str) -> tuple[str, dict]:
    """(pad, body) voor `POST` bij Nuki. De acties staan vast; `action` is de code van Nuki (1 unlock, 2 lock, 3 unlatch, 4 lock 'n' go, 5 unlatch met lock 'n' go)."""
    if aid not in ACTIONS:
        raise ValueError("onbekende actie")
    return f"/smartlock/{lid}/action", {"action": ACTIONS[aid][0]}


def problem(data: Any) -> str:
    """De melding van Nuki uit een foutantwoord, of een lege tekst."""
    if isinstance(data, dict):
        for k in ("detailMessage", "message", "error"):
            v = data.get(k)
            if isinstance(v, str) and v:
                return v[:200]
            if isinstance(v, dict) and isinstance(v.get("message"), str):
                return v["message"][:200]
    return ""


# ------------------------------------------------------------ het netwerk

async def request(session: aiohttp.ClientSession, token: str, method: str, path: str, body=None, timeout: float = TIMEOUT) -> tuple[int, Any]:
    """Een verzoek aan Nuki. Geeft (status, JSON). Een geweigerde token en te veel verzoeken zijn een NukiError met een leesbare reden; de rest geeft de status terug."""
    url = BASE + path
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with session.request(method, url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            # De hele body lezen: `content.read(n)` geeft maar terug wat er al binnen is (zie hue.py)
            parts, size = [], 0
            async for chunk in r.content.iter_chunked(16384):
                size += len(chunk)
                if size > MAX_BODY:
                    raise NukiError("net", "antwoord van Nuki te groot")
                parts.append(chunk)
            raw = b"".join(parts)
            status = r.status
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise NukiError("net", f"geen verbinding met Nuki: {e or 'time-out'}") from None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    if status == 401:
        raise NukiError("key", "Nuki weigert de token: maak een nieuwe aan op web.nuki.io (Integraties) en koppel opnieuw", status)
    if status == 403:
        raise NukiError("key", "Nuki weigert dit: heeft de token het recht om sloten te lezen en te bedienen (smartlock en smartlock.action)?", status)
    if status == 429:
        raise NukiError("rate", "te veel verzoeken bij Nuki (maximaal 100 per minuut): even wachten", status)
    return status, data
