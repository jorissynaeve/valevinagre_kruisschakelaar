"""Brink-ventilatie (Flair, Ubiflux Vigor, ...) via de cloud van Brink Home als HUISDEV-apparaat: een ventilatie-unit is een blok `e:<id>`.

De oude API van Brink Home (`UserLogon`, `GetAppGuiDescriptionForGateway`, wat JavaLox gebruikte) bestaat niet meer. De nieuwe webapp (`/app/`) praat met
`/portal/api/v1.1/` en logt in via IdentityServer (OpenID Connect, code-flow met PKCE, een openbare client `spa`). Dit is gereconstrueerd uit de openbare JavaScript
van die app, een openbare inlogpagina en een ingelogde sessie van de eigenaar. Brink documenteert de API niet: hij kan zonder waarschuwing weer veranderen.

  lezen     `GET systems?pageSize=..`                      de systemen (`systemShareId`, `gatewayState`: 0 vergrendeld, 1 offline, 2 online)
            `GET systems/<id>/uidescription`               de componenten met per parameter `valueId`, `name`, `value` (tekst), `valueState`, `readWrite`
  schrijven `PUT systems/<id>/parameter-values`            `{"writeValues": [{"valueId", "value" (tekst), "state"}]}`; asynchroon, de waarde komt bij de volgende poll
  inloggen  GET authorize (PKCE) -> inlogformulier (`Input.Username`, `Input.Password`, een anti-forgery token) -> doorverwijzing met `code` -> token

Parameters hebben per installatie een ander `valueId`, maar een vaste `name` (Duitse sleutel): daarop wordt gekoppeld, niet op het id.

Een punt is alleen bekend als `valueState` goed is (0 of 1 = wordt gewijzigd); fout, buiten dienst en geen geldige data zijn onbekend, nooit een gok.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html as _html
import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp

BASE = "https://www.brink-home.com"
API = "/portal/api/v1.1/"
AUTHORITY = "/idsrv"
CLIENT_ID = "spa"
REDIRECT = "https://www.brink-home.com/app/"
SCOPE = "openid api role locale"
TIMEOUT = 20
MAX_BODY = 4 << 20
GATEWAY_ONLINE = 2  # `gatewayState`: 0 vergrendeld, 1 offline, 2 online (de app zelf: enum Locked, Offline, Online)
STATE_OK = (0, 1)  # `valueState`: 0 goed, 1 wordt gewijzigd; 4 fout, 5 buiten dienst, 6 geen geldige data

LEVELS = {0: "Stand 0", 1: "Stand 1", 2: "Stand 2", 3: "Stand 3"}
MODES = {0: "Automatisch", 1: "Handbediening", 2: "Vakantie", 3: "Party", 4: "Nachtventilatie"}
BYPASS_MODES = {0: "Automatisch", 1: "Gesloten", 2: "Open"}
BYPASS_STATES = {0: "Initialiseren", 1: "Opent", 2: "Sluit", 3: "Open", 4: "Gesloten"}
PREHEATER = {0: "Uit", 1: "Automatisch", 2: "Huidige stand", 3: "Maximaal"}
ACTIVE = {0: "Stand-by", 1: "Bootloader", 2: "Fout", 3: "Blokkeringsfout", 4: "Handmatig", 5: "Vakantie", 6: "Nachtventilatie", 7: "Party", 8: "Boost", 9: "Boost", 10: "CO₂",
          11: "eBus", 12: "Modbus", 13: "Portaal", 14: "Lokaal"}

# naam van de parameter bij Brink -> (punt, label, soort, eigenschappen)
POINTS: list[tuple[str, str, str, str, dict]] = [
    ("Lüftungsstufe", "level", "Ventilatiestand", "enum", {"writable": True, "options": LEVELS}),
    ("Betriebsart", "mode", "Bedrijfsmodus", "enum", {"writable": True, "options": MODES}),
    ("Restlaufzeit Betriebsartfunktion", "remaining", "Resterende looptijd", "int", {"unit": "min"}),
    ("Funktion der Bypass Klappe", "bypass_mode", "Bypass-modus", "enum", {"writable": True, "options": BYPASS_MODES}),
    ("Ist-Wert Luftdurchsatz Zuluft", "supply_flow", "Debiet toevoer", "int", {"unit": "m³/h", "state": "measurement"}),
    ("Ist-Wert Luftdurchsatz Abluft", "exhaust_flow", "Debiet afvoer", "int", {"unit": "m³/h", "state": "measurement"}),
    ("Frischlufttemperatur", "outdoor_temp", "Buitentemperatuur", "float", {"unit": "°C", "cls": "temperature", "state": "measurement"}),
    ("Zulufttemperatur", "supply_temp", "Toevoertemperatuur", "float", {"unit": "°C", "cls": "temperature", "state": "measurement"}),
    ("Status Bypassklappe", "bypass_state", "Bypassklep", "enum", {"options": BYPASS_STATES}),
    ("Status Vorheizregister", "preheater", "Voorverwarming", "enum", {"options": PREHEATER}),
    ("Status Filtermeldung", "filter_dirty", "Filter vuil", "bool", {}),
    ("Anzahl der Tage seit Filterreset", "filter_days", "Dagen sinds filterreset", "int", {"unit": "d"}),
    ("Aktive Regelung", "active", "Actieve regeling", "enum", {"options": ACTIVE, "diag": True}),
]
KEY_PARAM = "Lüftungsstufe"  # een component met deze parameter is een ventilatie-unit


class BrinkError(Exception):
    """Een fout bij Brink Home, met een zin die zegt wat er mis is. `code` is `auth` (e-mail of wachtwoord onjuist), `token` (de sessie is verlopen), `rate`, `forbidden`
    of `net`."""

    def __init__(self, code: str, reason: str, status: int = 0) -> None:
        super().__init__(reason)
        self.code, self.reason, self.status = code, reason, status


# ------------------------------------------------------------ de lijst lezen

def systems(data: Any) -> dict[str, dict]:
    """`GET systems?pageSize=..` als {id: {name, state}}. Het id is `systemShareId`, dat in alle andere paden staat."""
    out = {}
    for x in data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), list) else []:
        sid = x.get("systemShareId") if isinstance(x, dict) else None
        if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0:
            name = x.get("systemName")
            gw = x.get("gatewayState")
            out[str(sid)] = {"name": name.strip()[:60] if isinstance(name, str) and name.strip() else "Brink-systeem", "state": gw if isinstance(gw, int) and not isinstance(gw, bool) else None}
    return out


def components(ui: Any) -> dict[str, dict]:
    """`systems/<id>/uidescription` als {componentId: {name, params: {parameternaam: parameter}}}. Een component staat in de boom soms op meer dan een plek:
    zijn parameters worden samengevoegd."""
    out: dict[str, dict] = {}

    def walk(items):
        for n in items if isinstance(items, list) else []:
            if not isinstance(n, dict):
                continue
            cid = n.get("componentId")
            if isinstance(cid, int) and not isinstance(cid, bool):
                c = out.setdefault(str(cid), {"name": "", "params": {}})
                if isinstance(n.get("name"), str) and not c["name"]:
                    c["name"] = n["name"].strip()[:60]
                for g in n.get("parameterGroups") if isinstance(n.get("parameterGroups"), list) else []:
                    for p in (g.get("parameters") if isinstance(g, dict) and isinstance(g.get("parameters"), list) else []):
                        if isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("valueId"), int):
                            c["params"][p["name"]] = p
            walk(n.get("navigationItems"))

    root = ui.get("root") if isinstance(ui, dict) else None
    walk(root.get("navigationItems") if isinstance(root, dict) else None)
    return out


def is_ventilation(comp: dict) -> bool:
    return KEY_PARAM in comp.get("params", {})


def ventilation_units(comps: dict[str, dict]) -> dict[str, dict]:
    return {cid: c for cid, c in comps.items() if is_ventilation(c)}


def hid_of(sid: str, cid: str) -> str:
    return f"brink-{sid}-{cid}"[:32]


def account_id(email: str) -> str:
    return "k_" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:6]


# ------------------------------------------------------------ van component naar punten

def _pt(pid: str, label: str, kind: str, **kw) -> dict:
    opts = kw.pop("options", None)
    return {"id": pid, "label": label, "kind": kind, "unit": "", "decimals": 1 if kind == "float" else 0, "min": None, "max": None, "step": None,
            "options": [{"v": k, "l": v} for k, v in opts.items()] if opts else [], "writable": False, "danger": False, "diag": False, "group": "", "cls": "", "state": "", "stale": 0,
            "confirm": "", **kw}


def describe(comp: dict, name: str = "") -> dict:
    """De descriptor van een ventilatie-unit in de vorm van `espdevhub.clean_descriptor` (zonder device-velden). Een parameter die de unit niet heeft, geeft geen punt."""
    pts = [_pt(pid, label, kind, **extra) for pname, pid, label, kind, extra in POINTS if pname in comp["params"]]
    sw = comp["params"].get("softwareLabel", {}).get("value")
    title = comp["params"].get("deviceTypeTitle", {}).get("value")
    body = json.dumps(pts, sort_keys=True, separators=(",", ":"))
    return {"proto": "1.0", "rev": hashlib.sha1(body.encode()).hexdigest()[:8], "type": "Ventilatie", "name": name or (title if isinstance(title, str) and title else comp["name"]) or "Ventilatie",
            "fw": sw[:40] if isinstance(sw, str) else "", "lib": "", "points": pts, "actions": []}


def _good(p: dict) -> bool:
    return p.get("valueState") in STATE_OK and isinstance(p.get("value"), str)


def values(comp: dict) -> dict[str, Any]:
    """De waarde per punt, of None. Waarden komen van Brink als tekst; een parameter met een foute of ontbrekende staat is onbekend."""
    out: dict[str, Any] = {}
    for pname, pid, _label, kind, extra in POINTS:
        p = comp["params"].get(pname)
        if p is None:
            continue
        v = None
        if _good(p):
            try:
                x = float(p["value"])
            except ValueError:
                x = None
            if x is not None and x == x:
                if kind == "bool":
                    v = x != 0
                elif kind == "float":
                    v = x
                else:
                    v = int(round(x))
                    if "options" in extra and v not in extra["options"]:
                        v = None  # een stand die we niet kennen: onbekend, nooit een gok
        out[pid] = v
    return out


def write_request(sid: str, comp: dict, pid: str, value) -> tuple[str, dict]:
    """(pad, body) voor `PUT` bij Brink. De waarde gaat als tekst, met de staat die de parameter nu heeft (zoals de app doet)."""
    for pname, pid2, _label, kind, extra in POINTS:
        if pid2 == pid and extra.get("writable") and pname in comp["params"]:
            p = comp["params"][pname]
            if p.get("readWrite") not in (2, 3):
                raise ValueError("Brink laat dit niet instellen")
            if "options" in extra and int(value) not in extra["options"]:
                raise ValueError("geen toegestane keuze")
            return f"{API}systems/{sid}/parameter-values", {"writeValues": [{"valueId": p["valueId"], "value": str(int(value)), "state": p.get("valueState") if isinstance(p.get("valueState"), int) else 0}]}
    raise ValueError("dit punt is niet instelbaar")


# ------------------------------------------------------------ inloggen en verzoeken

def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def login_form(page: str) -> tuple[str, dict] | None:
    """(actie, verborgen velden) van het inlogformulier op de pagina van IdentityServer, of None. Het formulier heet `Input.Username` en heeft een anti-forgery token."""
    for m in re.finditer(r"<form\b([^>]*)>(.*?)</form>", page, re.S | re.I):
        attrs, body = m.group(1), m.group(2)
        if "Input.Username" not in body or "Input.Password" not in body:
            continue
        action = re.search(r'action="([^"]*)"', attrs, re.I)
        hidden = {}
        for i in re.finditer(r"<input\b[^>]*>", body, re.I):
            t = i.group(0)
            nm, ty, val = re.search(r'name="([^"]*)"', t), re.search(r'type="([^"]*)"', t), re.search(r'value="([^"]*)"', t)
            if nm and ty and ty.group(1).lower() == "hidden" and nm.group(1) != "Input.RememberMe":
                hidden[nm.group(1)] = _html.unescape(val.group(1)) if val else ""
        if "__RequestVerificationToken" in hidden:
            return _html.unescape(action.group(1)) if action else "", hidden
    return None


async def _read(r: aiohttp.ClientResponse) -> bytes:
    parts, size = [], 0
    async for chunk in r.content.iter_chunked(16384):  # de hele body lezen (zie hue.py)
        size += len(chunk)
        if size > MAX_BODY:
            raise BrinkError("net", "antwoord van Brink Home te groot")
        parts.append(chunk)
    return b"".join(parts)


async def _walk(session: aiohttp.ClientSession, url: str, method: str = "GET", data: dict | None = None) -> tuple[str, str, str]:
    """Doorverwijzingen volgen tot de pagina waar het om gaat. ('code', url met code, '') als IdentityServer terugverwijst naar de app; ('page', url, tekst) anders."""
    for _ in range(12):
        async with session.request(method, url, data=data, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status in (301, 302, 303, 307, 308):
                nxt = urljoin(str(r.url), r.headers.get("Location", ""))
                if nxt.startswith(REDIRECT):
                    return "code", nxt, ""
                url, method, data = nxt, "GET", None
                continue
            if r.status != 200:
                raise BrinkError("net", f"Brink Home antwoordt met HTTP {r.status}", r.status)
            return "page", str(r.url), (await _read(r)).decode("utf-8", "replace")
    raise BrinkError("net", "te veel doorverwijzingen bij het inloggen")


async def login(session: aiohttp.ClientSession, email: str, password: str) -> tuple[str, int]:
    """Inloggen zoals de webapp doet (code-flow met PKCE) en een toegangstoken halen. Geeft (token, geldig in seconden). `session` heeft een cookiejar nodig (unsafe voor tests).
    Een onjuist wachtwoord is BrinkError("auth"): probeer het dan niet steeds opnieuw, want een account kan daarop worden geblokkeerd."""
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(12)
    q = urlencode({"client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPE, "state": state, "nonce": secrets.token_urlsafe(12),
                   "code_challenge": challenge, "code_challenge_method": "S256"})
    try:
        kind, url, page = await _walk(session, f"{BASE}{AUTHORITY}/connect/authorize?{q}")
        if kind == "page":
            form = login_form(page)
            if form is None:
                raise BrinkError("net", "Brink Home toont een inlogpagina die ik niet herken (is er iets veranderd?)")
            action, hidden = form
            kind, url, page = await _walk(session, urljoin(url, action), "POST", {**hidden, "Input.Username": email, "Input.Password": password, "Input.RememberMe": "false"})
            if kind == "page":
                raise BrinkError("auth", "Brink Home weigert de e-mail of het wachtwoord")
        qs = parse_qs(urlparse(url).query)
        if qs.get("state", [""])[0] != state or not qs.get("code"):
            raise BrinkError("net", f"Brink Home gaf geen code terug ({qs.get('error', ['onbekende fout'])[0]})")
        async with session.post(f"{BASE}{AUTHORITY}/connect/token", data={"grant_type": "authorization_code", "client_id": CLIENT_ID, "code": qs["code"][0], "redirect_uri": REDIRECT,
                                                                       "code_verifier": verifier}, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            raw = await _read(r)
            status = r.status
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise BrinkError("net", f"geen verbinding met Brink Home: {e or 'time-out'}") from None
    try:
        tok = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        tok = {}
    if status != 200 or not isinstance(tok, dict) or not isinstance(tok.get("access_token"), str):
        raise BrinkError("net", f"Brink Home geeft geen toegangstoken (HTTP {status})", status)
    exp = tok.get("expires_in")
    return tok["access_token"], exp if isinstance(exp, int) and not isinstance(exp, bool) and exp > 0 else 3600


async def request(session: aiohttp.ClientSession, token: str, method: str, path: str, body=None, timeout: float = TIMEOUT) -> tuple[int, Any]:
    """Een verzoek aan de API. Geeft (status, JSON). Een verlopen sessie (401), een geweigerd recht en te veel verzoeken zijn een BrinkError."""
    try:
        async with session.request(method, BASE + path, json=body, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            raw = await _read(r)
            status = r.status
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise BrinkError("net", f"geen verbinding met Brink Home: {e or 'time-out'}") from None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    if status == 401:
        raise BrinkError("token", "de sessie bij Brink Home is verlopen", status)
    if status == 403:
        raise BrinkError("forbidden", "Brink Home weigert dit voor dit account (geen recht op dit systeem of deze parameter)", status)
    if status == 429:
        raise BrinkError("rate", "te veel verzoeken bij Brink Home: even wachten", status)
    return status, data
