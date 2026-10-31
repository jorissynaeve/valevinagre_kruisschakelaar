"""MQTT-discovery voor Home Assistant, als pure functies: model + engine -> {topic: payload}.

Per node (een apparaat in HA):
  light   per licht in het model (relais), schakelt via CMD_LIGHT, toestand uit LIGHT_STATE
  event   per knop ("Knop 1"): HA ziet welke schakelaar is ingedrukt, ook zonder dat er iets mee gebeurt
  sensor  firmware-versie (diagnostisch)
  binary_sensor  "Configuratie wijkt af" (diagnostisch), aan als een node zijn config kwijt is
"""
from __future__ import annotations

import json

from .engine import Engine, fw_str

PREFIX = "kruisschakelaars"
DISCOVERY = "homeassistant"
BRIDGE_STATUS = f"{PREFIX}/status"


def t_node_status(node: int) -> str:
    return f"{PREFIX}/node/{node}/status"


def t_light_state(node: int, relay: int) -> str:
    return f"{PREFIX}/light/{node}/{relay}/state"


def t_light_set(node: int, relay: int) -> str:
    return f"{PREFIX}/light/{node}/{relay}/set"


def t_button(node: int, btn: int) -> str:
    return f"{PREFIX}/node/{node}/button/{btn}"


def t_fw(node: int) -> str:
    return f"{PREFIX}/node/{node}/fw"


def t_cfg_problem(node: int) -> str:
    return f"{PREFIX}/node/{node}/cfg_problem"


LIGHT_SET_FILTER = f"{PREFIX}/light/+/+/set"

CFG_PROBLEM = ("drift", "failed")


def _device(engine: Engine, node: int) -> dict:
    info = engine.nodes.get(node)
    m = engine.model.nodes.get(node, {})
    d = {
        "identifiers": [f"kk_{node}"],
        "name": m.get("name") or f"Node {node}",
        "manufacturer": "Huisconfig",
        "model": "Extern apparaat (via HA)" if info and info.virtual else "ESP-NOW bridge" if info and info.bridge else "ESP-NOW node",
    }
    if info and info.fw:
        d["sw_version"] = fw_str(info.fw)
    if m.get("room"):
        d["suggested_area"] = m["room"]
    return d


def _avail(node: int) -> dict:
    return {"availability": [{"topic": BRIDGE_STATUS}, {"topic": t_node_status(node)}], "availability_mode": "all"}


def _topic(domain: str, oid: str) -> str:
    return f"{DISCOVERY}/{domain}/{oid}/config"


def configs(engine: Engine) -> dict[str, dict]:
    """Alle discovery-berichten die nu zouden moeten bestaan."""
    out: dict[str, dict] = {}
    model = engine.model
    nodes = set(engine.nodes) | {l.node for l in model.lights.values()}
    for n in sorted(nodes):
        dev = _device(engine, n)
        base = {"device": dev, **_avail(n)}
        for (ln, relay), l in sorted(model.lights.items()):
            if ln != n or not l.enabled:
                continue
            oid = f"kk_{n}_{relay}"
            out[_topic("light", oid)] = {
                **base,
                "name": l.name or f"Relais {relay + 1}",
                "unique_id": oid,
                "command_topic": t_light_set(n, relay),
                "state_topic": t_light_state(n, relay),
                "payload_on": "ON",
                "payload_off": "OFF",
            }
        info = engine.nodes.get(n)
        if info is None or info.virtual:  # een extern apparaat staat al in HA (local-tuya): alleen zijn lichten
            continue
        for b in range(info.nbtn or 0):
            oid = f"kk_{n}_b{b}"
            out[_topic("event", oid)] = {
                **base,
                "name": f"Knop {b + 1}",
                "unique_id": oid,
                "state_topic": t_button(n, b),
                "event_types": ["press"],
                "device_class": "button",
            }
        out[_topic("sensor", f"kk_{n}_fw")] = {
            **base,
            "name": "Firmware",
            "unique_id": f"kk_{n}_fw",
            "state_topic": t_fw(n),
            "entity_category": "diagnostic",
            "enabled_by_default": False,
        }
        out[_topic("binary_sensor", f"kk_{n}_cfg")] = {
            **base,
            "name": "Configuratie wijkt af",
            "unique_id": f"kk_{n}_cfg",
            "state_topic": t_cfg_problem(n),
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "problem",
            "entity_category": "diagnostic",
        }
    return out


def states(engine: Engine) -> dict[str, tuple[str, bool]]:
    """Retained toestanden: {topic: (payload, retain)}. Onbekende toestanden worden niet gepubliceerd."""
    out: dict[str, tuple[str, bool]] = {}
    nodes = set(engine.nodes) | {l.node for l in engine.model.lights.values()}
    for n in nodes:
        info = engine.nodes.get(n)
        out[t_node_status(n)] = ("online" if info and info.online else "offline", True)
        if not info:
            continue
        for i, r in enumerate(info.relays):
            if r.on is not None:
                out[t_light_state(n, i)] = ("ON" if r.on else "OFF", True)
        if info.virtual:
            continue
        if info.fw:
            out[t_fw(n)] = (fw_str(info.fw), True)
        out[t_cfg_problem(n)] = ("ON" if info.cfg_status in CFG_PROBLEM else "OFF", True)
    return out


def encode(cfg: dict) -> str:
    return json.dumps(cfg, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
