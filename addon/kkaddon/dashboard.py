"""De tab "Lampen" in de Overview van HA: alle lampen van de add-on per kamer, als pure functie (model + registers -> Lovelace-view).

De add-on beheert alleen deze ene tab (habridge.py zet hem in de bestaande Overview, de andere tabs blijven ongemoeid): bij elke
wijziging in het model of in de registers van HA wordt hij opnieuw opgebouwd. Handmatige aanpassingen aan die tab worden dus overschreven."""
from __future__ import annotations

from .model import Model

VIEW_PATH = "huisconfig-lampen"  # hiermee herkent de add-on zijn eigen tab
TITLE = "Lampen"
ICON = "mdi:lightbulb-group"
NO_ROOM = "Zonder kamer"


def unique_id(node: int, relay: int) -> str:
    return f"kk_{node}_{relay}"  # zoals in discovery.py


def build(model: Model, entities: dict[str, str], areas: dict[str, str]) -> dict:
    """De view (tab). `entities`: unique_id -> entity_id van de MQTT-lichten in HA; `areas`: unique_id -> naam van het gebied in HA.

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
    return {"title": TITLE, "path": VIEW_PATH, "icon": ICON, "cards": cards}
