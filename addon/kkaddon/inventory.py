"""Eén lijst van alle apparaten van het huis, uit alle bronnen die de add-on kent: naam, soort, protocol, adres, versie.

Bronnen: ESP-NOW-nodes (engine), externe apparaten via HA (virtuele nodes, met het adres en de versie uit het
HA-apparatenregister), ESPHome-apparaten in HA die nog niet in de add-on staan, Tuya lokaal, Gree-airco's, zelfbouwapparaten (HUISDEV),
NSPanels en openHASP-schermen. Alleen lezen: dit verandert niets aan de configuratie."""
from __future__ import annotations

from typing import Any

from .engine import Engine

PLATFORM_NL = {"esphome": "ESPHome", "tuya": "Tuya (cloud)", "localtuya": "Tuya (local-tuya)", "tasmota": "Tasmota", "mqtt": "MQTT"}


def _platform(p: list[str]) -> str:
    return " + ".join(PLATFORM_NL.get(x, x) for x in p) if p else "Home Assistant"


def build(engine: Engine, ha: dict | None) -> list[dict[str, Any]]:
    """`ha` is wat `HaBridge.inventory()` geeft (of None zonder HA-koppeling)."""
    ha_devs = (ha or {}).get("devices") or []
    by_entity = {e: d for d in ha_devs for e in d["entities"]}
    claimed: set[str] = set()
    rows: list[dict[str, Any]] = []

    def row(key: str, name: str, kind: str, protocol: str, ip: str = "", version: str = "", online: bool | None = None, room: str = "", note: str = "") -> None:
        rows.append({"key": key, "name": name, "room": room, "kind": kind, "protocol": protocol, "ip": ip, "version": version, "online": online, "note": note})

    for n in sorted(engine.nodes.values(), key=lambda n: n.id):
        v = engine.node_view(n)
        if n.virtual:
            devs = {by_entity[g["entity"]]["id"]: by_entity[g["entity"]] for g in v["gangs"] or [] if g["entity"] in by_entity}
            d = next(iter(devs.values()), None)
            claimed.update(devs)
            row(f"n{n.id}", v["name"] or f"Extern {n.id}", f"schakelaar, {len(v['gangs'] or [])} gangen",
                _platform(d["platforms"]) if d else "Home Assistant", d["ip"] if d else "", d["version"] if d else "", v["online"], v["room"],
                f"in de add-on als node {n.id}")
        else:
            kind = "bridge" if v["bridge"] else f"{len(v['relays'])} relais" + (f", {v['nbtn']} knoppen" if v["nbtn"] else "")
            row(f"n{n.id}", v["name"] or f"Node {n.id}", kind, "ESP-NOW", "", v["fw"], v["online"], v["room"], f"node {n.id}")

    for d in ha_devs:  # ESPHome in HA, nog niet als node in de add-on
        if d["id"] in claimed or "esphome" not in d["platforms"]:
            continue
        row(f"h{d['id']}", d["name"], d["model"] or "ESPHome-apparaat", _platform(d["platforms"]), d["ip"], d["version"], None, d["area"], "alleen in Home Assistant")

    for t in (engine.tuya_view() if engine.tuya_view else []):
        row(f"t{t['id']}", t["name"], t.get("product") or "Tuya-apparaat", "Tuya lokaal" + (f" (protocol {t['version']})" if t.get("version") not in (None, "", "auto") else ""),
            t.get("host", ""), t.get("version") if t.get("version") not in ("auto", None) else "", t["online"], "", t.get("error") or "")
    for a in (engine.airco_view() if engine.airco_view else []):
        row(f"a{a['id']}", a["name"], "airco", "Gree (UDP)", a.get("host", ""), a.get("firmware") or "", a["online"], "", a.get("error") or "")
    for e in (engine.espdev_view() if engine.espdev_view else []):
        note = {"upgrade": f"upgrade beschikbaar (actueel protocol {e['current']}, zie protocol/huisdev/HUISDEV.md)", "unsupported": "niet ondersteund: werk het apparaat of de add-on bij"}.get(e["proto_state"], "")
        if e.get("driver") == "brink":
            row(f"e{e['id']}", e["name"], "Brink-ventilatie", "Brink Home (cloud)", "www.brink-home.com", e.get("fw") or "", e["online"], "", e.get("error") or "")
            continue
        if e.get("driver") == "nuki":
            row(f"e{e['id']}", e["name"], "Nuki-slot", "Nuki Web API", "api.nuki.io", e.get("fw") or "", e["online"], "", e.get("error") or "")
            continue
        if e.get("driver") == "hue":
            row(f"e{e['id']}", e["name"], e["type"] or "Hue-apparaat", "Hue (CLIP v2)", e.get("host", ""), e.get("fw") or "", e["online"], "", e.get("error") or "")
            continue
        if e.get("driver") == "esphome":
            row(f"e{e['id']}", e["name"], "ESPHome-apparaat", "ESPHome (webserver)", e.get("host", ""), e.get("fw") or "", e["online"], "", e.get("error") or "")
            continue
        row(f"e{e['id']}", e["name"], e["type"] or "zelfbouwapparaat", f"HUISDEV {e['proto']}", e.get("host", ""), e.get("fw") or "", e["online"], "", note or e.get("error") or "")
    for p in (engine.panel_view() if engine.panel_view else []):
        row(f"p{p['id']}", p["name"], "NSPanel", "UDP + Tasmota", p.get("host", ""), "", p["online"], "", p.get("error") or "")
    for s in (engine.screen_view() if engine.screen_view else []):
        row(f"s{s['id']}", s["name"], "scherm (openHASP)", "MQTT", "", "", s["online"], "", s.get("error") or "")
    return rows
