"""Philips Hue-bridge (CLIP v2, lokaal) als HUISDEV-apparaat: een Hue-lamp, -stopcontact of -bewegingsmelder is een blok `e:<id>`.

Het pure deel staat hier: de resources van de bridge lezen, er per apparaat een descriptor van maken zoals HUISDEV §5 hem beschrijft, de
waarden eruit halen en een opdracht omzetten naar het verzoek van de bridge. Het netwerk (HTTPS met vastgezet certificaat, de eventstroom)
en de opslag zitten in espdevhub.py, zoals bij ESPHome.

  lamp          een `light` met `dimming`      aan (bool, instelbaar), helderheid (1-100, instelbaar), bereikbaar (diagnose)
  stopcontact   een `light` zonder `dimming`   aan (bool, instelbaar), bereikbaar (diagnose)
  melder        een `motion`-dienst            beweging (bool), temperatuur, lichtniveau (lux), batterij (%), bereikbaar (diagnose)

Een stopcontact herken je niet aan zijn archetype (dat kiest de gebruiker in de Hue-app: `plug`, `string_light`, `classic_bulb`), maar aan het
ontbreken van `dimming`. Wat we niet kennen (knoppen, de bridge zelf, kamers, scènes) wordt niet aangeboden.

Een apparaat dat de bridge niet bereikt (`zigbee_connectivity` is niet `connected`: een lamp die uit staat, een lege batterij) heeft geen
bekende waarden: de bridge onthoudt zijn laatste stand, maar dat is een gok. Alleen `reach` is dan nog bekend.

Punt-ids liggen vast in de code (`on`, `bri`, `motion`, `temp`, `lux`, `battery`, `reach`); het id van het apparaat komt van de UUID van Hue,
nooit van zijn naam (er bestaan twee lampen met dezelfde naam).
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import ssl
from typing import Any

import aiohttp

from .esphome import Sse  # dezelfde server-sent-events als ESPHome; een regel die met `:` begint is commentaar en telt niet mee

API_PORT = 443
TIMEOUT = 8  # s per verzoek aan de bridge
MAX_BODY = 8 << 20  # bytes; de volledige lijst van deze bridge is 108 KB, een grote installatie een paar MB
RESOURCE = "/clip/v2/resource"
STREAM = "/eventstream/clip/v2"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FP_RE = re.compile(r"^[0-9a-f]{64}$")
BRIDGE_ID_RE = re.compile(r"^[0-9A-F]{16}$")
KINDS = {"lamp": "Hue-lamp", "plug": "Hue-stopcontact", "sensor": "Hue-bewegingsmelder", "room": "Hue-kamer"}


class HueError(Exception):
    """Een fout bij de bridge, met een zin die zegt wat er mis is. `code` is `cert` (certificaat veranderd), `link` (knop niet ingedrukt),
    `key` (sleutel geweigerd), `rate` (te druk) of `net`."""

    def __init__(self, code: str, reason: str, status: int = 0) -> None:
        super().__init__(reason)
        self.code, self.reason, self.status = code, reason, status


# ------------------------------------------------------------ de resources

def merge(old: dict, new: dict) -> dict:
    """Een gewijzigd stuk van een resource in de oude leggen. Een dict wordt samengevoegd (een update bevat maar wat veranderde), al het andere vervangen."""
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(old.get(k), dict):
            merge(old[k], v)
        else:
            old[k] = v
    return old


def index(data: Any) -> dict[str, dict]:
    """`data` uit `GET /clip/v2/resource` als {id: resource}. Alles wat geen resource met een id en type is, vervalt."""
    return {r["id"]: r for r in (data if isinstance(data, list) else []) if isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("type"), str)}


def events(data: str) -> list[tuple[str, dict]]:
    """Een `data:`-regel van de eventstroom als [(soort, resource)]. Soort is update, add of delete; `error` en alles wat niet past telt niet mee."""
    try:
        j = json.loads(data)
    except ValueError:
        return []
    out = []
    for env in j if isinstance(j, list) else []:
        if not isinstance(env, dict) or env.get("type") not in ("update", "add", "delete"):
            continue
        for r in env.get("data") if isinstance(env.get("data"), list) else []:
            if isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("type"), str):
                out.append((env["type"], r))
    return out


def apply(store: dict[str, dict], kind: str, r: dict) -> None:
    """Een gebeurtenis in de resources. Een update van iets dat we niet kennen laten we vallen: een halve resource is erger dan geen."""
    if kind == "delete":
        store.pop(r["id"], None)
    elif kind == "add":
        store[r["id"]] = r
    elif r["id"] in store and store[r["id"]].get("type") == r["type"]:
        merge(store[r["id"]], r)


def _svc(dev: dict, store: dict, rtype: str) -> dict | None:
    for s in dev.get("services") if isinstance(dev.get("services"), list) else []:
        if isinstance(s, dict) and s.get("rtype") == rtype and isinstance(s.get("rid"), str) and s["rid"] in store:
            return store[s["rid"]]
    return None


def kind_of(dev: dict, store: dict) -> str | None:
    """lamp, plug, sensor of room; None voor wat we niet aanbieden (een knop, de bridge, een apparaat zonder lichtdienst of beweging, een kamer zonder lampen)."""
    if dev.get("type") == "room":
        return "room" if _svc(dev, store, "grouped_light") is not None else None
    if dev.get("type") != "device":
        return None
    light = _svc(dev, store, "light")
    if light is not None:
        return "lamp" if isinstance(light.get("dimming"), dict) else "plug"
    return "sensor" if _svc(dev, store, "motion") is not None else None


def _name(dev: dict) -> str:
    n = (dev.get("metadata") or {}).get("name")
    return n.strip()[:60] if isinstance(n, str) and n.strip() else "Hue-apparaat"


def hid_of(rid: str) -> str:
    """Het HUISDEV-id (`hue-3f9a1c2b7d`) van een Hue-apparaat: de eerste tien tekens van zijn UUID."""
    return "hue-" + rid.replace("-", "")[:10]


def devices(store: dict[str, dict]) -> list[dict]:
    """Wat de bridge aanbiedt, voor het menu: [{rid, hid, name, kind, kindname, model, fw}] op naam, alleen wat we ondersteunen."""
    out = []
    for rid, d in store.items():
        k = kind_of(d, store)
        if k and UUID_RE.match(rid):
            pd = d.get("product_data") if isinstance(d.get("product_data"), dict) else {}
            out.append({"rid": rid, "hid": hid_of(rid), "name": _name(d), "kind": k, "kindname": KINDS[k], "model": str(pd.get("model_id") or "")[:30], "fw": str(pd.get("software_version") or "")[:40]})
    return sorted(out, key=lambda x: (x["name"].lower(), x["rid"]))


def room_scenes(room: dict, store: dict) -> list[tuple[str, str]]:
    """[(rid, naam)] van de scènes van een kamer, op naam."""
    out = []
    for rid, r in store.items():
        g = r.get("group")
        if r.get("type") == "scene" and isinstance(g, dict) and g.get("rid") == room.get("id") and UUID_RE.match(rid):
            n = (r.get("metadata") or {}).get("name")
            out.append((rid, n.strip()[:36] if isinstance(n, str) and n.strip() else "Scène"))
    return sorted(out, key=lambda x: (x[1].lower(), x[0]))


def assign_scenes(numbers: dict, room: dict, store: dict) -> bool:
    """Elke scène van de kamer een vast nummer (1, 2, ...), want Loxone onthoudt een scène als getal. Een nummer wordt nooit hergebruikt, ook niet als de scène verdwijnt
    of een andere naam krijgt (het id van de scène blijft). True als er een scène bijkwam."""
    nxt, changed = max(numbers.values(), default=0) + 1, False
    for rid, _ in room_scenes(room, store):
        if rid not in numbers:
            numbers[rid] = nxt
            nxt += 1
            changed = True
    return changed


def skipped(store: dict[str, dict]) -> list[str]:
    """De namen van apparaten die we niet aanbieden (knoppen, dimmers), zodat het in de zijbalk staat en niet stil ontbreekt."""
    return sorted(_name(d) for d in store.values() if d.get("type") == "device" and kind_of(d, store) is None and not _is_bridge(d, store))


def _is_bridge(dev: dict, store: dict) -> bool:
    return _svc(dev, store, "bridge") is not None


# ------------------------------------------------------------ van apparaat naar punten

def _pt(pid: str, label: str, kind: str, **kw) -> dict:
    return {"id": pid, "label": label, "kind": kind, "unit": "", "decimals": 1 if kind == "float" else 0, "min": None, "max": None, "step": None, "options": [], "writable": False,
            "danger": False, "diag": False, "group": "", "cls": "", "state": "", "stale": 0, "confirm": "", **kw}


def describe(dev: dict, store: dict, name: str = "", numbers: dict | None = None) -> dict | None:
    """De descriptor van een Hue-apparaat in de vorm van `espdevhub.clean_descriptor` (zonder device-velden), of None als we het niet kennen.
    De punten hangen af van de diensten die het apparaat heeft: een melder zonder temperatuurdienst krijgt geen temperatuurpunt."""
    k = kind_of(dev, store)
    if not k:
        return None
    pts: list[dict] = []
    if k == "room":
        scenes = sorted(((numbers or {}).get(rid), n) for rid, n in room_scenes(dev, store) if (numbers or {}).get(rid))
        pts.append(_pt("on", "Aan", "bool", writable=True))
        pts.append(_pt("bri", "Helderheid", "int", writable=True, unit="%", min=0, max=100, step=1))  # 0 is uit
        pts.append(_pt("scene", "Scène", "enum", writable=True, options=[{"v": 0, "l": "geen / uit"}] + [{"v": v, "l": n} for v, n in scenes[:63]]))
    elif k in ("lamp", "plug"):
        pts.append(_pt("on", "Aan", "bool", writable=True))
        if k == "lamp":
            pts.append(_pt("bri", "Helderheid", "int", writable=True, unit="%", min=1, max=100, step=1))
    else:
        pts.append(_pt("motion", "Beweging", "bool"))
        if _svc(dev, store, "temperature") is not None:
            pts.append(_pt("temp", "Temperatuur", "float", unit="°C", cls="temperature", state="measurement"))
        if _svc(dev, store, "light_level") is not None:
            pts.append(_pt("lux", "Lichtniveau", "float", unit="lx", cls="illuminance", state="measurement"))
        if _svc(dev, store, "device_power") is not None:
            pts.append(_pt("battery", "Batterij", "int", unit="%", cls="battery", state="measurement", min=0, max=100, step=1))
    pts.append(_pt("reach", "Bereikbaar", "bool", diag=True))
    pd = dev.get("product_data") if isinstance(dev.get("product_data"), dict) else {}
    body = json.dumps(pts, sort_keys=True, separators=(",", ":"))
    return {"proto": "1.0", "rev": hashlib.sha1(body.encode()).hexdigest()[:8], "id": hid_of(dev["id"]), "type": KINDS[k], "name": name or _name(dev), "fw": str(pd.get("software_version") or "")[:40],
            "lib": "", "points": pts, "actions": [], "model": str(pd.get("model_id") or "")[:30]}


def lux(raw) -> float | None:
    """`light_level` van Hue is 10000·log10(lux)+1; 0 en lager is donker."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != raw:
        return None
    return 0.0 if raw <= 0 else round(10 ** ((raw - 1) / 10000), 1)


def reachable(dev: dict, store: dict) -> bool:
    z = _svc(dev, store, "zigbee_connectivity")
    return z is None or z.get("status") == "connected"


def room_reach(room: dict, store: dict) -> bool:
    """Een kamer is bereikbaar als minstens één van haar lampen dat is; anders is de stand van de groep een gok."""
    for c in room.get("children") if isinstance(room.get("children"), list) else []:
        d = store.get(c.get("rid")) if isinstance(c, dict) else None
        if d and d.get("type") == "device" and _svc(d, store, "light") is not None and reachable(d, store):
            return True
    return False


def _room_values(room: dict, store: dict, numbers: dict) -> dict[str, Any]:
    out: dict[str, Any] = {"reach": room_reach(room, store)}
    g = _svc(room, store, "grouped_light")
    if not out["reach"] or g is None:
        return out
    on = (g.get("on") or {}).get("on")
    on = on if isinstance(on, bool) else None
    b = (g.get("dimming") or {}).get("brightness")
    out["on"] = on
    out["bri"] = 0 if on is False else int(round(b)) if isinstance(b, (int, float)) and not isinstance(b, bool) and b == b else None
    active = [numbers[rid] for rid, _ in room_scenes(room, store) if rid in numbers and (store[rid].get("status") or {}).get("active") not in (None, "inactive")]
    out["scene"] = None if on is None else 0 if on is False or not active else min(active)
    return out


def values(dev: dict, store: dict, numbers: dict | None = None) -> dict[str, Any]:
    """De waarde per punt, of None (onbekend). Een apparaat dat de bridge niet bereikt heeft alleen `reach`."""
    if dev.get("type") == "room":
        return _room_values(dev, store, numbers or {})
    out: dict[str, Any] = {"reach": reachable(dev, store)}
    if not out["reach"]:
        return out
    light = _svc(dev, store, "light")
    if light is not None:
        on = (light.get("on") or {}).get("on")
        out["on"] = on if isinstance(on, bool) else None
        if isinstance(light.get("dimming"), dict):  # een stopcontact dimt niet en heeft geen helderheid
            b = light["dimming"].get("brightness")
            out["bri"] = int(round(b)) if isinstance(b, (int, float)) and not isinstance(b, bool) and b == b else None
    m = _svc(dev, store, "motion")
    if m is not None:
        mm = m.get("motion") or {}
        out["motion"] = mm.get("motion") if mm.get("motion_valid") is True and isinstance(mm.get("motion"), bool) else None
    t = _svc(dev, store, "temperature")
    if t is not None:
        tt = t.get("temperature") or {}
        v = tt.get("temperature")
        out["temp"] = float(v) if tt.get("temperature_valid") is True and isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None
    ll = _svc(dev, store, "light_level")
    if ll is not None:
        l2 = ll.get("light") or {}
        out["lux"] = lux(l2.get("light_level")) if l2.get("light_level_valid") is True else None
    p = _svc(dev, store, "device_power")
    if p is not None:
        v = (p.get("power_state") or {}).get("battery_level")
        out["battery"] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100 else None
    return out


def point_request(dev: dict, store: dict, pid: str, value, numbers: dict | None = None) -> tuple[str, dict]:
    """(pad, body) voor `PUT` bij de bridge. Opdrachten zijn absoluut: `on` is aan of uit, nooit een toggle (HUISDEV §7).
    Een kamer: `on` en `bri` gaan naar haar groep (0 is uit), een scène wordt geactiveerd met haar eigen verzoek."""
    if dev.get("type") == "room":
        g = _svc(dev, store, "grouped_light")
        if g is None:
            raise ValueError("deze kamer heeft geen lampen")
        path = f"{RESOURCE}/grouped_light/{g['id']}"
        if pid == "on":
            return path, {"on": {"on": bool(value)}}
        if pid == "bri":
            v = float(value)
            return path, ({"on": {"on": False}} if v <= 0 else {"on": {"on": True}, "dimming": {"brightness": v}})
        if pid == "scene":
            n = int(value)
            if n == 0:
                return path, {"on": {"on": False}}
            rid = next((r for r, _ in room_scenes(dev, store) if (numbers or {}).get(r) == n), None)
            if rid is None:
                raise ValueError("deze scène bestaat niet (meer) in deze kamer")
            return f"{RESOURCE}/scene/{rid}", {"recall": {"action": "active"}}
        raise ValueError("dit punt is niet instelbaar")
    light = _svc(dev, store, "light")
    if light is None or pid not in ("on", "bri"):
        raise ValueError("dit punt is niet instelbaar")
    if pid == "on":
        return f"{RESOURCE}/light/{light['id']}", {"on": {"on": bool(value)}}
    if not isinstance(light.get("dimming"), dict):
        raise ValueError("deze lamp dimt niet")
    return f"{RESOURCE}/light/{light['id']}", {"dimming": {"brightness": float(value)}}


def problems(data: Any) -> str:
    """De melding van de bridge uit `errors` van een antwoord, of een lege tekst."""
    errs = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errs, list) and errs:
        e = errs[0]
        return str(e.get("description") if isinstance(e, dict) else e)[:200]
    return ""


# ------------------------------------------------------------ het netwerk

@functools.lru_cache(maxsize=16)
def ssl_for(fp: str):
    """Het certificaat van de bridge is van Signify en dus door geen publieke autoriteit getekend: we zetten de vingerafdruk vast bij het koppelen.
    Gecached: aiohttp houdt een verbinding open per (adres, ssl-object) en een `Fingerprint` is alleen aan zichzelf gelijk, dus anders kreeg elk verzoek een nieuwe TLS-handdruk."""
    return aiohttp.Fingerprint(bytes.fromhex(fp))


async def fetch_fingerprint(host: str, port: int = API_PORT) -> str:
    """SHA-256 van het certificaat dat de bridge nu toont (zonder iets te verifiëren: dit is het moment waarop we vertrouwen)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), TIMEOUT)
    except (OSError, ssl.SSLError, asyncio.TimeoutError) as e:
        raise HueError("net", f"geen verbinding met {host}: {e or 'time-out'}") from None
    try:
        der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        w.close()
    if not der:
        raise HueError("net", f"{host} toont geen certificaat")
    return hashlib.sha256(der).hexdigest()


async def request(session: aiohttp.ClientSession, br: dict, method: str, path: str, body=None, key: bool = True, timeout: float = TIMEOUT) -> tuple[int, Any]:
    """Een verzoek aan de bridge, over HTTPS met de vastgezette vingerafdruk. Geeft (status, JSON). Elke fout is een HueError met een leesbare reden."""
    url = f"https://{br['host']}:{br['port']}{path}"
    headers = {"hue-application-key": br["key"]} if key and br.get("key") else {}
    try:
        async with session.request(method, url, json=body, headers=headers, ssl=ssl_for(br["fp"]), timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            # De hele body lezen: `content.read(n)` geeft maar terug wat er al binnen is, en de lijst van een bridge (100 KB en meer) komt in stukken.
            parts, size = [], 0
            async for chunk in r.content.iter_chunked(16384):
                size += len(chunk)
                if size > MAX_BODY:
                    raise HueError("net", "antwoord van de bridge te groot")
                parts.append(chunk)
            raw = b"".join(parts)
            status = r.status
    except aiohttp.ServerFingerprintMismatch:
        raise HueError("cert", f"het certificaat van de bridge op {br['host']} is veranderd (bridge vervangen of teruggezet?): vertrouw hem opnieuw") from None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise HueError("net", f"geen verbinding met {br['host']}: {e or 'time-out'}") from None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    if status in (401, 403):
        raise HueError("key", "de bridge weigert de sleutel: koppel de bridge opnieuw", status)
    if status == 429:
        raise HueError("rate", "de bridge krijgt te veel opdrachten: probeer het zo opnieuw", status)
    return status, data


async def config(session: aiohttp.ClientSession, host: str, port: int, fp: str) -> dict:
    """`GET /api/config` (zonder sleutel): de bridge noemt zichzelf. Een ander toestel dat op dit adres antwoordt, is geen Hue-bridge."""
    status, data = await request(session, {"host": host, "port": port, "fp": fp}, "GET", "/api/config", key=False)
    if status != 200 or not isinstance(data, dict) or not BRIDGE_ID_RE.match(str(data.get("bridgeid") or "").upper()) or not str(data.get("modelid") or "").startswith("BSB"):
        raise HueError("net", f"{host} is geen Hue-bridge (of een te oude)")
    return data


async def pair(session: aiohttp.ClientSession, host: str, port: int, fp: str, app: str = "huisconfig#addon") -> str:
    """Een sleutel vragen. Lukt alleen binnen 30 seconden na het indrukken van de ronde knop op de bridge."""
    status, data = await request(session, {"host": host, "port": port, "fp": fp}, "POST", "/api", {"devicetype": app, "generateclientkey": True}, key=False)
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    if isinstance(first.get("success"), dict) and isinstance(first["success"].get("username"), str):
        return first["success"]["username"]
    err = first.get("error") if isinstance(first.get("error"), dict) else {}
    if err.get("type") == 101:
        raise HueError("link", "druk op de ronde knop op de bridge en probeer het binnen 30 seconden opnieuw")
    raise HueError("net", f"de bridge geeft geen sleutel ({err.get('description') or status})")
