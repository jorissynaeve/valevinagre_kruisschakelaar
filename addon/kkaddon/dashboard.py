"""De tab "Lampen" in de Overview van HA: alle lampen uit het schema per kamer, als pure functie (model + registers -> Lovelace-view).

De add-on beheert alleen deze ene tab (habridge.py zet hem in de bestaande Overview, de andere tabs blijven ongemoeid): bij elke
wijziging in het model of in de registers van HA wordt hij opnieuw opgebouwd. Handmatige aanpassingen aan die tab worden dus overschreven.

De lampen zijn de lampblokken van het schema, met de kamer waarop ze staan. Een lamp is in HA:
  * een groep (blok met HA-entiteiten): die entiteiten zelf,
  * een gang van een extern apparaat (bv. een Dingtian-relais): de entiteit van die gang, ook als het licht van de add-on verborgen is,
  * een licht van een node: het `light` dat de add-on via MQTT-discovery aanbiedt."""
from __future__ import annotations

from . import schema
from .model import Model

VIEW_PATH = "huisconfig-lampen"  # hiermee herkent de add-on zijn eigen tab
OLD_DASHBOARD = "huisconfig-lampen"  # het zijbalk-dashboard van 0.18.0, dat de add-on zelf maakte en nu opruimt
TITLE = "Lampen"
ICON = "mdi:lightbulb-group"
NO_ROOM = "Zonder kamer"


def unique_id(node: int, relay: int) -> str:
    return f"kk_{node}_{relay}"  # zoals in discovery.py


def _entities(model: Model, b: dict, uids: dict[str, str]) -> list[str]:
    """De HA-entiteiten van een lampblok."""
    if b["source"] == "ha":
        return [o["ref"]["entity"] for o in b["outputs"] if o["ref"].get("entity")]
    node, relay = b["node"], b["relay"]
    gangs = model.virtual.get(node)
    if gangs is not None:  # extern apparaat: de entiteit van de gang, niet het (standaard verborgen) licht van de add-on
        return [gangs[relay]["entity"]] if relay < len(gangs) else []
    light = model.lights.get((node, relay))
    ent = uids.get(unique_id(node, relay))
    return [ent] if light and light.enabled and ent else []


def build(model: Model, snap: dict, uids: dict[str, str], areas: dict[str, str], known: set[str] | None = None) -> tuple[dict, list[str]]:
    """De view (tab). `snap`: de momentopname van de engine; `uids`: unique_id -> entity_id van de MQTT-lichten in HA;
    `areas`: entity_id -> gebied in HA; `known`: de entiteiten die HA kent (een onbekende geeft in HA een waarschuwingsrij en blijft weg).
    Geeft de view en de weggelaten entiteiten. De kamer is de pagina van het schema waarop de lamp staat, anders het gebied in HA."""
    v = schema.view(model, snap)
    pages = {p["id"]: p["name"] for p in v["pages"]}
    blocks = sorted((b for b in v["blocks"] if b["kind"] == "lamp"), key=lambda b: (b["source"] != "ha", b["name"].lower()))
    rooms: dict[str, list[tuple[str, str]]] = {}
    seen: set[str] = set()
    missing: list[str] = []
    for b in blocks:
        ents = [e for e in _entities(model, b, uids) if e not in seen]
        if known is not None:
            missing += [e for e in ents if e not in known]
            ents = [e for e in ents if e in known]
        for i, ent in enumerate(ents):
            seen.add(ent)
            room = pages.get(b.get("page")) or areas.get(ent) or NO_ROOM
            name = b["name"] if len(ents) == 1 else f"{b['name']} ({i + 1})"
            rooms.setdefault(room, []).append((name, ent))
    cards = []
    for room in sorted(rooms, key=lambda r: (r == NO_ROOM, r.lower())):
        cards.append({
            "type": "entities",
            "title": room,
            "show_header_toggle": True,
            "entities": [{"entity": ent, "name": name} for name, ent in sorted(rooms[room], key=lambda x: (x[0].lower(), x[1]))],
        })
    return {"title": TITLE, "path": VIEW_PATH, "icon": ICON, "cards": cards}, sorted(set(missing))
