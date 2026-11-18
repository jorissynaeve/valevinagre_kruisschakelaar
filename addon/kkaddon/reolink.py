"""Reolink-camera (de HTTP-API `api.cgi` op de camera zelf) als HUISDEV-apparaat: een camera is een blok `e:<id>`.

Het pure deel staat hier: de verzoeken en antwoorden van de camera, de punten die een camera heeft, hun waarden en een opdracht voor de schijnwerper.
Het netwerk (inloggen, pollen, het webhook) en de opslag zitten in espdevhub.py, zoals bij Hue en Nuki.

  beweging     `GetMdState`   value.state 0 of 1
  persoon      `GetAiState`   value.people.alarm_state, alleen als de camera `support: 1` meldt
  voertuig     `GetAiState`   value.vehicle.alarm_state, idem
  schijnwerper `GetWhiteLed`  value.WhiteLed.state (0/1) en .bright (0 tot 100), bedienen met `SetWhiteLed`; alleen als de camera de opdracht kent

De camera geeft elk verzoek een array antwoorden terug, in de volgorde van de verzoeken: `{"cmd", "code", "value"}`, of bij een fout `{"cmd", "code": 1,
"error": {"rspCode", "detail"}}`. Een fout bij één opdracht (een camera zonder schijnwerper) maakt van alleen die punten "onbekend".

Bron van de vorm: de bibliotheek reolink_aio en de documentatie van Reolink. Niet tegen een echte camera geprobeerd (DESIGN.md §41).
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
from typing import Any

API = "/cgi-bin/api.cgi"
TIMEOUT = 6  # s per verzoek
AI_KEYS = {"people": ("person", "Persoon"), "vehicle": ("vehicle", "Voertuig")}  # sleutel in GetAiState -> (punt, label)
KIND = "Reolink-camera"


class ReoError(Exception):
    """Een fout bij de camera, met een zin die zegt wat er mis is. `kind` is `auth` (aanmelding geweigerd), `token` (de sessie is verlopen: opnieuw aanmelden),
    `net` (geen verbinding) of `http` (een antwoord dat geen API is)."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind, self.reason = kind, reason


# ------------------------------------------------------------ verzoeken

def login_body(user: str, password: str) -> list[dict]:
    return [{"cmd": "Login", "action": 0, "param": {"User": {"userName": user, "password": password}}}]


def _cmd(name: str, ch: int, **param) -> dict:
    return {"cmd": name, "action": 0, "param": {"channel": ch, **param}}


def probe_commands(ch: int = 0) -> list[dict]:
    """Wat de camera is en kan: één verzoek met alles wat we willen weten."""
    return [{"cmd": "GetDevInfo", "action": 0, "param": {}}, _cmd("GetMdState", ch), _cmd("GetAiState", ch), _cmd("GetWhiteLed", ch)]


def poll_commands(caps: dict) -> list[dict]:
    ch = caps["channel"]
    out = [_cmd("GetMdState", ch)]
    if caps["ai"]:
        out.append(_cmd("GetAiState", ch))
    if caps["led"]:
        out.append(_cmd("GetWhiteLed", ch))
    return out


# ------------------------------------------------------------ antwoorden

def _entries(data: Any) -> list[dict]:
    if not isinstance(data, list):
        raise ReoError("http", "de camera antwoordt niet zoals een Reolink-API")
    return [e if isinstance(e, dict) else {} for e in data]


def _err(e: dict) -> tuple[int, str]:
    x = e.get("error") if isinstance(e.get("error"), dict) else {}
    rsp = x.get("rspCode")
    return (rsp if isinstance(rsp, int) and not isinstance(rsp, bool) else 0), str(x.get("detail") or "")[:80]


def _login_needed(e: dict) -> bool:
    rsp, detail = _err(e)
    return rsp == -6 or "login" in detail.lower() and "fail" not in detail.lower()


def parse_login(data: Any) -> tuple[str, int]:
    """(token, geldigheid in s) uit het antwoord op `Login`. ReoError("auth") als de camera de aanmelding weigert."""
    es = _entries(data)
    e = es[0] if es else {}
    tok = ((e.get("value") or {}).get("Token") or {}) if e.get("code") == 0 else {}
    if not isinstance(tok.get("name"), str) or not tok["name"]:
        rsp, detail = _err(e)
        raise ReoError("auth", "de camera weigert de aanmelding" + (f": {detail}" if detail else " (gebruikersnaam of wachtwoord?)"))
    lease = tok.get("leaseTime")
    return tok["name"], int(lease) if isinstance(lease, (int, float)) and not isinstance(lease, bool) and lease > 0 else 3600


def _value(es: list[dict], i: int, key: str | None = None):
    """`value` van antwoord `i`, of None bij een fout van die opdracht. Een verlopen sessie is wel een fout voor het geheel."""
    if i >= len(es):
        return None
    e = es[i]
    if e.get("code") == 0 and isinstance(e.get("value"), dict):
        v = e["value"]
        return v.get(key) if key else v
    if e and _login_needed(e):
        raise ReoError("token", "de sessie bij de camera is verlopen")
    return None


def _bool(x) -> bool | None:
    return bool(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x in (0, 1) else None


def caps_from_probe(data: Any, ch: int = 0) -> dict:
    """Wat deze camera heeft: model, naam, serienummer, firmware, welke AI-soorten (`support` 1) en of hij een schijnwerper kent."""
    es = _entries(data)
    info = _value(es, 0, "DevInfo")
    info = info if isinstance(info, dict) else {}
    ai = _value(es, 2)
    ai = ai if isinstance(ai, dict) else {}
    led = _value(es, 3, "WhiteLed")
    t = lambda k, n: str(info.get(k) or "").strip()[:n]  # noqa: E731
    return {"channel": ch, "model": t("model", 40), "name": t("name", 60), "serial": re.sub(r"[^A-Za-z0-9]", "", t("serial", 40)), "fw": t("firmVer", 40),
            "ai": [k for k in AI_KEYS if isinstance(ai.get(k), dict) and ai[k].get("support") == 1], "led": isinstance(led, dict)}


def clean_caps(raw: Any) -> dict:
    """De opgeslagen caps van een camera, gecontroleerd (het bestand kan met de hand zijn bewerkt)."""
    raw = raw if isinstance(raw, dict) else {}
    ch = raw.get("channel")
    t = lambda k, n: str(raw.get(k) or "").strip()[:n]  # noqa: E731
    return {"channel": ch if isinstance(ch, int) and not isinstance(ch, bool) and 0 <= ch <= 64 else 0, "model": t("model", 40), "name": t("name", 60), "serial": re.sub(r"[^A-Za-z0-9]", "", t("serial", 40)),
            "fw": t("fw", 40), "ai": [k for k in (raw.get("ai") if isinstance(raw.get("ai"), list) else []) if k in AI_KEYS], "led": raw.get("led") is True}


# ------------------------------------------------------------ punten en waarden

def _pt(pid: str, label: str, kind: str, **kw) -> dict:
    return {"id": pid, "label": label, "kind": kind, "unit": "", "decimals": 0, "min": None, "max": None, "step": None, "options": [], "writable": False,
            "danger": False, "diag": False, "group": "", "cls": "", "state": "", "stale": 0, "confirm": "", **kw}


def describe(caps: dict, hid: str, name: str) -> dict:
    """De descriptor in de vorm van `espdevhub.clean_descriptor` (zonder device-velden). De punten hangen af van wat de camera kan."""
    pts = [_pt("motion", "Beweging", "bool")]
    for k in caps["ai"]:
        pid, label = AI_KEYS[k]
        pts.append(_pt(pid, label, "bool"))
    if caps["led"]:
        pts.append(_pt("light", "Schijnwerper", "bool", writable=True))
        pts.append(_pt("bri", "Helderheid schijnwerper", "int", writable=True, unit="%", min=0, max=100, step=1))  # 0 is uit
    body = json.dumps(pts, sort_keys=True, separators=(",", ":"))
    return {"proto": "1.0", "rev": hashlib.sha1(body.encode()).hexdigest()[:8], "id": hid, "type": KIND, "name": name, "fw": caps["fw"], "lib": "", "points": pts, "actions": [],
            "model": caps["model"]}


def values(data: Any, caps: dict) -> tuple[dict[str, Any], dict]:
    """(waarde per punt of None, de laatste stand van de schijnwerper). Een punt waarvan de opdracht een fout gaf, is onbekend."""
    es = _entries(data)
    out: dict[str, Any] = {"motion": _bool(_value(es, 0, "state"))}
    i = 1
    if caps["ai"]:
        ai = _value(es, i) or {}
        i += 1
        for k in caps["ai"]:
            x = ai.get(k) if isinstance(ai.get(k), dict) else {}
            out[AI_KEYS[k][0]] = _bool(x.get("alarm_state")) if x.get("support") == 1 else None
    led: dict = {}
    if caps["led"]:
        w = _value(es, i, "WhiteLed")
        led = w if isinstance(w, dict) else {}
        on = _bool(led.get("state"))
        b = led.get("bright")
        out["light"] = on
        out["bri"] = 0 if on is False else int(round(b)) if isinstance(b, (int, float)) and not isinstance(b, bool) and 0 <= b <= 100 else None
    return out, led


def point_request(caps: dict, pid: str, value, led: dict) -> dict:
    """De opdracht `SetWhiteLed` voor een punt. `led` is de laatste stand die de camera meldde: de modus blijft zoals hij was. Absoluut, nooit een toggle."""
    if not caps["led"] or pid not in ("light", "bri"):
        raise ValueError("dit punt is niet instelbaar")
    mode = led.get("mode") if isinstance(led.get("mode"), int) and not isinstance(led.get("mode"), bool) and 0 <= led["mode"] <= 3 else 1
    bright = led.get("bright") if isinstance(led.get("bright"), (int, float)) and not isinstance(led.get("bright"), bool) and 1 <= led["bright"] <= 100 else 100
    if pid == "light":
        state, bright = (1 if value else 0), int(bright)
    else:
        v = float(value)
        state, bright = (0, int(bright)) if v <= 0 else (1, int(round(v)))
    return {"cmd": "SetWhiteLed", "param": {"WhiteLed": {"channel": caps["channel"], "state": state, "mode": mode, "bright": bright}}}


def command_error(data: Any) -> str:
    """De melding van de camera uit het antwoord op een opdracht, of een lege tekst als het lukte."""
    es = _entries(data)
    if not es:
        return "geen antwoord van de camera"
    e = es[0]
    if e.get("code") == 0:
        v = e.get("value")
        rsp = v.get("rspCode") if isinstance(v, dict) else None
        return "" if rsp in (None, 200) else f"de camera meldt {rsp}"
    if _login_needed(e):
        raise ReoError("token", "de sessie bij de camera is verlopen")
    rsp, detail = _err(e)
    return f"de camera weigert dit: {detail or rsp or 'onbekende fout'}"


# ------------------------------------------------------------ het webhook

def local_ip(target: str) -> str:
    """Het IP van deze machine zoals de camera het ziet (de weg naar de camera), voor het adres van het webhook."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""
