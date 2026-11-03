"""Het HA-dashboard "Lampen": alle lampen van de add-on per kamer, als pure functie (model + registers -> Lovelace-config).

De add-on beheert dit dashboard zelf (habridge.py schrijft het): bij elke wijziging in het model of in de registers van HA
wordt het opnieuw opgebouwd. Handmatige aanpassingen in dat dashboard worden dus overschreven."""
from __future__ import annotations

from .model import Model

URL_PATH = "huisconfig-lampen"  # een dashboard-pad moet een koppelteken bevatten
TITLE = "Lampen"
ICON = "mdi:lightbulb-group"
NO_ROOM = "Zonder kamer"


def unique_id(node: int, relay: int) -> str:
    return f"kk_{node}_{relay}"  # zoals in discovery.py


def build(model: Model, entities: dict[str, str], areas: dict[str, str]) -> dict:
    """`entities`: unique_id -> entity_id van de MQTT-lichten in HA; `areas`: unique_id -> naam van het gebied in HA.

    Een licht dat HA nog niet kent (net toegevoegd) blijft weg tot HA het registreert; het gebied uit HA gaat voor op de kamer uit het model."""
    rooms: dict[str, list[tuple[str, str]]] = {}
    for (node, relay), l in model.lights.items():
        if not l.enabled:
            continue
        uid = unique_id(node, relay)
        ent = entities.get(uid)
        if not ent:
            continue
        room = areas.get(uid) or (model.nodes.get(node, {}).get("room") or "").strip() or NO_ROOM
        rooms.setdefault(room, []).append((l.name or f"Relais {relay + 1}", ent))
    cards = []
    for room in sorted(rooms, key=lambda r: (r == NO_ROOM, r.lower())):
        cards.append({
            "type": "entities",
            "title": room,
            "show_header_toggle": True,
            "entities": [{"entity": ent} for _, ent in sorted(rooms[room], key=lambda x: (x[0].lower(), x[1]))],
        })
    return {"title": TITLE, "views": [{"title": TITLE, "path": "lampen", "icon": ICON, "cards": cards}]}
