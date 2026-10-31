"""Het blokkenschema, zoals een pagina in Loxone Config: per ruimte een tabblad met lamp- en schakelaar-blokken en draden.

  * Een **lamp-blok** is een kring (`g:<id>`) of een licht op het netwerk (`l<node>.<relais>`).
    Ingangen: de touches die de lamp bedienen. Uitgangen: de relais die de lamp echt schakelen.
  * Een **schakelaar-blok** is een toestel met per gang een touch en een relais: een node (`n<id>`, ook een extern
    apparaat) of een HA-apparaat (`d:<id>`).

Er staat geen tweede waarheid in: een draad *is* een lid van een kring of een trigger van een licht. Trek je een draad,
dan verandert precies dat (`wire`). Het volgen zelf blijft zoals het was: alle leden van een kring volgen elkaar, ook
een touch-relais, want daar hangt zijn LED aan. Wat in het schema zelf wordt bewaard (ruimtes, plekken, schakelaars)
is alleen weergave en raakt de schakelingen nooit.
"""
from __future__ import annotations

import copy
import re
from collections import Counter

from .model import MAX_SWITCH_GANGS, Model, ModelError
from .proto import is_virtual

NO_ROOM = "Centraal"
# Hoogte en kolommen van de automatische indeling; de hoogtes zijn dezelfde als in static/schema.js
HEAD, ROW, FOOT, GAP = 42, 24, 10, 24
COL_IN, COL_LAMP, COL_OUT = 40, 460, 920
GRID_X, GRID_STEP, GRID_COLS = 20, 600, 2  # een pagina zonder lampen (Centraal): een raster, met naast elk blok plaats voor de verwijzingen
NOT_A_GANG = re.compile(r"(^|_)(led|leds|all|all_relays|status|backlight|buzzer)(_|$)")  # zoals in de import van app.js
NUMBER_END = re.compile(r"_?(?:relay|relais|gang|channel|ch|switch|lamp|light|output|r|l)?_?\d+$")


def _natural(s: str) -> list:
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")[:40] or "x"


def _short_labels(entities: list[str]) -> dict[str, str]:
    """Een gang heet naar wat zijn id onderscheidt: `relay_1`, niet `r4b01_sonoff_4ch_r4_01_relay_1`."""
    ids = [e.split(".", 1)[1] for e in entities]
    pre = ids[0] if ids else ""
    for i in ids:
        while not i.startswith(pre):
            pre = pre[:-1]
    pre = pre[: pre.rfind("_") + 1]
    return {e: (i[len(pre):] or i) for e, i in zip(entities, ids)}


# ---------------------------------------------------------------- van model naar blokken


def _gang_owners(model: Model) -> dict[str, tuple[str, int]]:
    """Entiteit -> (blok, gang). Een extern apparaat gaat voor: zo verhuist een gang vanzelf mee als je een toestel importeert."""
    own: dict[str, tuple[str, int]] = {}
    for vid, gangs in model.virtual.items():
        for i, g in enumerate(gangs):
            own.setdefault(g["entity"], (f"n{vid}", i))
    for sid, s in model.schema["switches"].items():
        for i, g in enumerate(s["gangs"]):
            own.setdefault(g["entity"], (f"d:{sid}", i))
    return own


def _switch_blocks(model: Model, snap: dict, owners: dict, states: dict) -> list[dict]:
    out = []
    infos = {n["id"]: n for n in snap["nodes"]}
    used = {t.node for l in model.lights.values() for t in l.triggers} | {k[0] for k in model.lights}
    for nid in sorted(set(infos) | used | set(model.virtual)):
        info = infos.get(nid) or {}
        relays = info.get("relays") or []
        if nid in model.virtual:
            gangs = [{"i": i, "label": g["name"] or f"Gang {i + 1}", "entity": g["entity"], "touch": True, "relay": True,
                      "on": relays[i]["on"] if i < len(relays) else None} for i, g in enumerate(model.virtual[nid])]
        else:
            known_btn = [t.btn + 1 for l in model.lights.values() for t in l.triggers if t.node == nid]
            known_rel = [k[1] + 1 for k in model.lights if k[0] == nid]
            nbtn = max([info.get("nbtn") or 0, *known_btn])
            nrel = max([len(relays), *known_rel])
            gangs = [{"i": i, "label": f"Gang {i + 1}", "entity": None, "touch": i < nbtn, "relay": i < nrel,
                      "on": relays[i]["on"] if i < len(relays) else None} for i in range(max(nbtn, nrel))]
        if gangs:
            out.append({"key": f"n{nid}", "kind": "switch", "source": "virtual" if is_virtual(nid) else "node", "node": nid,
                        "name": model.node_name(nid), "room": model.nodes.get(nid, {}).get("room", ""),
                        "online": bool(info.get("online")), "gangs": gangs})
    for sid, s in model.schema["switches"].items():
        key = f"d:{sid}"
        gangs = [{"i": i, "label": g["name"] or g["entity"], "entity": g["entity"], "touch": True, "relay": True, "on": states.get(g["entity"])}
                 for i, g in enumerate(s["gangs"])]
        if any(owners.get(g["entity"]) == (key, g["i"]) for g in gangs):  # anders is alles al naar een extern apparaat verhuisd
            out.append({"key": key, "kind": "switch", "source": "ha", "id": sid, "name": s["name"], "room": "", "online": True, "gangs": gangs})
    for b in out:  # de technische naam blijft zichtbaar naast een eigen naam
        b["tech"], b["alias"] = b["name"], model.schema["names"].get(b["key"], "")
        b["name"] = b["alias"] or b["tech"]
    return out


def _lamp_blocks(model: Model, snap: dict, owners: dict) -> list[dict]:
    out = []
    for g in snap["groups"]:
        lamps = [m for m in g["members"] if m["lamp"] and m["on"] is not None]
        basis = lamps or g["members"]
        on = None if all(m["on"] is None for m in basis) else any(m["on"] for m in basis)
        conns = {"in": [], "out": []}
        for m in g["members"]:
            sw, gang = owners.get(m["entity"], (None, None))
            conns["out" if m["lamp"] else "in"].append({"ref": {"entity": m["entity"]}, "sw": sw, "gang": gang, "on": m["on"], "text": m["entity"]})
        out.append({"key": f"g:{g['id']}", "kind": "lamp", "source": "ha", "id": g["id"], "name": g["name"], "tech": g["id"], "enabled": g["enabled"], "on": on,
                    "pending": False, "online": any(m["avail"] for m in g["members"]), "inputs": conns["in"], "outputs": conns["out"],
                    "disable": g.get("disable"), "blocked": g.get("blocked", False)})
    for l in snap["lights"]:
        inputs = [{"ref": {"node": t["node"], "btn": t["btn"]}, "mode": t["mode"], "sw": f"n{t['node']}", "gang": t["btn"], "on": None,
                   "text": f"{model.node_name(t['node'])} knop {t['btn'] + 1}"} for t in l["triggers"]]
        output = {"ref": {"node": l["node"], "relay": l["relay"]}, "sw": f"n{l['node']}", "gang": l["relay"], "on": l["on"], "fixed": True,
                  "text": f"{model.node_name(l['node'])} relais {l['relay'] + 1}"}
        out.append({"key": f"l{l['node']}.{l['relay']}", "kind": "lamp", "source": "node", "node": l["node"], "relay": l["relay"],
                    "name": l["name"] or f"{model.node_name(l['node'])} relais {l['relay'] + 1}",
                    "tech": f"{model.node_name(l['node'])} relais {l['relay'] + 1}", "enabled": True, "on": l["on"],
                    "pending": l["pending"], "online": l["online"], "inputs": inputs, "outputs": [output]})
    return out


def _timer_blocks(snap: dict) -> list[dict]:
    return [{"key": f"t:{t['id']}", "kind": "timer", "source": "timer", "id": t["id"], "name": t["name"], "tech": t["id"], "alias": "", "room": "",
             "online": True, "on": t["on"], "days": t["days"]} for t in snap.get("timers", [])]


def view(model: Model, snap: dict) -> dict:
    """Alles wat de pagina nodig heeft om het schema te tekenen."""
    states = {m["entity"]: m["on"] for g in snap["groups"] for m in g["members"]}
    owners = _gang_owners(model)
    blocks = _switch_blocks(model, snap, owners, states) + _lamp_blocks(model, snap, owners) + _timer_blocks(snap)
    names = {b["key"]: b for b in blocks if b["kind"] == "switch"}
    for b in blocks:
        for c in b.get("inputs", []) + b.get("outputs", []):
            sw = names.get(c["sw"])
            if sw is None:
                c["sw"] = c["gang"] = None
            elif c["gang"] < len(sw["gangs"]):
                c["label"] = sw["gangs"][c["gang"]]["label"]
                c["swName"] = sw["name"]
        pos = model.schema["blocks"].get(b["key"])
        b.update(pos if pos else {"page": None, "x": 0, "y": 0})
    return {"pages": model.schema["pages"], "blocks": sorted(blocks, key=lambda b: (b["kind"], b["name"].lower()))}


# ---------------------------------------------------------------- een draad trekken of weghalen


def _virtual_gang_of(model: Model, entity: str) -> tuple[int, int] | None:
    for vid, gangs in model.virtual.items():
        for i, g in enumerate(gangs):
            if g["entity"] == entity:
                return vid, i
    return None


def wire(model: Model, engine, lamp: str, direction: str, ref: dict, remove: bool = False) -> None:
    """De enige plek waar het schema de schakelingen verandert."""
    if direction not in ("in", "out") or not isinstance(ref, dict):
        raise ModelError("draad: richting moet in of out zijn")
    if lamp.startswith("g:"):
        _wire_group(model, lamp[2:], direction, ref, remove)
    else:
        m = re.fullmatch(r"l(\d+)\.(\d+)", lamp)
        if not m:
            raise ModelError("draad: onbekende lamp")
        _wire_light(model, engine, int(m[1]), int(m[2]), direction, ref, remove)


def _wire_group(model: Model, gid: str, direction: str, ref: dict, remove: bool) -> None:
    g = model.groups.get(gid)
    if g is None:
        raise ModelError("onbekende lamp")
    ent = ref.get("entity")
    if ent is None and is_virtual(ref.get("node") or 0):
        gangs = model.virtual.get(ref["node"]) or []
        i = ref.get("btn", ref.get("relay"))
        ent = gangs[i]["entity"] if isinstance(i, int) and 0 <= i < len(gangs) else None
    if not isinstance(ent, str):
        raise ModelError("Deze lamp loopt via Home Assistant; een geflashte node kan er nog niet op aansluiten. Maak de lamp vanaf een relais van de node.")
    ents, lamps = list(g["entities"]), list(g["lamp"])
    if remove:
        ents = [e for e in ents if e != ent]
        lamps = [e for e in lamps if e != ent]
    else:
        if ent not in ents:
            ents.append(ent)
        lamps = [e for e in lamps if e != ent] + ([ent] if direction == "out" else [])  # een lid is ingang of uitgang, nooit allebei
    model.set_group(gid, entities=ents, lamp=lamps)


def _wire_light(model: Model, engine, node: int, relay: int, direction: str, ref: dict, remove: bool) -> None:
    l = model.lights.get((node, relay))
    if l is None:
        raise ModelError("onbekende lamp")
    if direction == "out":
        raise ModelError("De uitgang van dit licht ligt vast: het is het relais van de node zelf.")
    src = (ref.get("node"), ref.get("btn"))
    if "entity" in ref:
        src = _virtual_gang_of(model, ref["entity"])
        if src is None:
            raise ModelError("Dit toestel staat nog niet op het netwerk. Importeer het eerst als extern apparaat (tabblad Nodes).")
    trig = [t.as_dict() for t in l.triggers if (t.node, t.btn) != src]
    if not remove:
        if is_virtual(node) and src == (node, relay):
            raise ModelError("Dit relais is de uitgang van deze lamp; zijn eigen touch schakelt het al.")
        trig.append({"node": src[0], "btn": src[1], "mode": ref.get("mode", "toggle")})
    engine.set_triggers(node, relay, trig)


# ---------------------------------------------------------------- blokken op de pagina's


def existing_blocks(model: Model, snap: dict) -> set[str]:
    return {b["key"] for b in view(model, snap)["blocks"]}


def place(model: Model, snap: dict, key: str, page, x=0, y=0) -> None:
    """Zet een blok op een pagina, of haalt het eraf (`page` None). Oude plekken van verdwenen blokken ruimen we meteen op."""
    known = existing_blocks(model, snap)
    if key not in known:
        raise ModelError(f"onbekend blok {key}")
    s = copy.deepcopy(model.schema)
    s["blocks"] = {k: v for k, v in s["blocks"].items() if k in known}
    if page is None:
        s["blocks"].pop(key, None)
    else:
        s["blocks"][key] = {"page": page, "x": x, "y": y}
    model.set_schema(s)


def set_disable(model: Model, lamp: str, timer) -> None:
    """De disable-ingang van een lamp. Een licht op het netwerk beslist op de node zelf en kan dit nog niet."""
    if not lamp.startswith("g:"):
        raise ModelError("Disable kan voorlopig alleen op een lamp via Home Assistant: een licht op het netwerk beslist op de node zelf.")
    if lamp[2:] not in model.groups:
        raise ModelError("onbekende lamp")
    model.set_group(lamp[2:], disable=timer)


def set_name(model: Model, snap: dict, key: str, name) -> None:
    """Eigen naam van een schakelaar-blok; leeg = weer de technische naam."""
    b = next((x for x in view(model, snap)["blocks"] if x["key"] == key), None)
    if b is None or b["kind"] != "switch":
        raise ModelError(f"onbekende schakelaar {key}")
    s = copy.deepcopy(model.schema)
    s["names"][key] = name
    model.set_schema(s)


def set_pages(model: Model, pages) -> None:
    """Toevoegen, hernoemen, herschikken en verwijderen. Blokken van een verwijderde ruimte gaan terug naar de lijst; de draden blijven."""
    if not isinstance(pages, list):
        raise ModelError("ruimtes: lijst verwacht")
    keep = {p.get("id") for p in pages if isinstance(p, dict)}
    s = copy.deepcopy(model.schema)
    s["pages"] = pages
    s["blocks"] = {k: v for k, v in s["blocks"].items() if v["page"] in keep}
    model.set_schema(s)


def save_switch(model: Model, sid: str | None, name, gangs) -> str:
    s = copy.deepcopy(model.schema)
    if sid is None:
        base = _slug(str(name or "schakelaar"))
        sid, n = base, 2
        while sid in s["switches"]:
            sid, n = f"{base}_{n}", n + 1
    elif sid not in s["switches"]:
        raise ModelError("onbekende schakelaar")
    s["switches"][sid] = {"name": name, "gangs": gangs}
    model.set_schema(s)
    return sid


def remove_switch(model: Model, sid: str) -> bool:
    if sid not in model.schema["switches"]:
        return False
    s = copy.deepcopy(model.schema)
    del s["switches"][sid]
    s["blocks"].pop(f"d:{sid}", None)
    s["names"].pop(f"d:{sid}", None)
    model.set_schema(s)
    return True


# ---------------------------------------------------------------- het huidige omzetten


def _new_switches(model: Model, devices: list[dict]) -> tuple[dict, dict]:
    """Maakt een schakelaar-blok voor elke entiteit van een kring die er nog geen heeft. Uit het HA-apparatenregister,
    of (zonder register) op de naam van de entiteit. Geeft ook terug in welke ruimte elk nieuw blok hoort."""
    switches = copy.deepcopy(model.schema["switches"])
    claimed = {g["entity"] for gs in model.virtual.values() for g in gs} | {g["entity"] for s in switches.values() for g in s["gangs"]}
    needed = [e for g in model.groups.values() for e in g["entities"] if e not in claimed]
    wanted = set(needed)
    rooms = {f"d:{_slug(str(d['id']))[:80]}": d.get("area") or "" for d in devices}  # ook van blokken die al bestaan
    def order(ents):
        return sorted(ents, key=lambda e: (0 if e.startswith("switch.") else 1, _natural(e)))
    for d in devices:
        ents = [e["entity_id"] for e in d.get("entities", [])]
        if not wanted.intersection(ents):
            continue
        ents = order(e for e in ents if e not in claimed and (e in wanted or not NOT_A_GANG.search(e.split(".", 1)[1])))[:MAX_SWITCH_GANGS]
        sid = _slug(str(d["id"]))[:80]
        labels = _short_labels(ents)
        switches[sid] = {"name": d.get("name") or sid, "gangs": [{"entity": e, "name": labels[e]} for e in ents]}
        claimed.update(ents)
    by_prefix: dict[str, list[str]] = {}
    for e in needed:
        if e not in claimed:
            obj = e.split(".", 1)[1]
            by_prefix.setdefault(NUMBER_END.sub("", obj) or obj, []).append(e)
    for pre, ents in by_prefix.items():
        for part in range(0, len(ents), MAX_SWITCH_GANGS):
            chunk = order(dict.fromkeys(ents[part:part + MAX_SWITCH_GANGS]))
            sid = _slug(pre) if part == 0 else f"{_slug(pre)}_{part // MAX_SWITCH_GANGS + 1}"
            labels = _short_labels(chunk)
            switches[sid] = {"name": pre.replace("_", " "), "gangs": [{"entity": e, "name": labels[e]} for e in chunk]}
    return switches, rooms


def _height(b: dict) -> int:
    if b["kind"] == "timer":
        return HEAD + ROW + FOOT
    rows = len(b["gangs"]) if b["kind"] == "switch" else max(len(b["inputs"]), len(b["outputs"])) + (2 if b["source"] == "ha" else 1)
    return HEAD + rows * ROW + FOOT


def _families(lamps: list[dict]) -> dict[str, str]:
    """Lampen die bij elkaar horen op naam: `Quarto philippe bed links` bij `Quarto philippe`, en `Buiten keuken` bij `Buiten terras`."""
    names = {b["key"]: b["name"] for b in lamps}
    out, bases = {}, set()
    for k, n in names.items():
        hits = [o for o in names.values() if n.casefold().startswith(o.casefold() + " ")]
        if hits:
            out[k] = max(hits, key=len)
            bases.add(out[k])
    rest = {k: n.split()[0] for k, n in names.items() if k not in out and n not in bases and " " in n}
    count = Counter(w.casefold() for w in rest.values())
    out.update({k: w.capitalize() for k, w in rest.items() if count[w.casefold()] > 1})
    return out


def _rooms(blocks: list[dict], rooms: dict[str, str], areas: set[str]) -> dict[str, str]:
    """In welke ruimte hoort elk blok. Een schakelaar: zijn eigen HA-gebied of de kamer van de node, anders Centraal (waar een
    toestel hangt valt niet te raden). Een lamp: zijn naamfamilie, anders een ruimte in zijn naam, anders waar de meeste van
    zijn touches zitten, anders heet de ruimte zoals de lamp. Een naamloos relais gaat naar Centraal."""
    out = {b["key"]: rooms.get(b["key"]) or b.get("room") or "" for b in blocks if b["kind"] == "switch"}
    named = sorted(areas | {r for r in out.values() if r}, key=len, reverse=True)
    lamps = [b for b in blocks if b["kind"] == "lamp"]
    fam = _families(lamps)
    def most(conns):
        c = Counter(out[x["sw"]] for x in conns if out.get(x["sw"]))
        return c.most_common(1)[0][0] if c else ""
    for b in lamps:
        hit = next((a for a in named if re.search(rf"\b{re.escape(a.casefold())}\b", b["name"].casefold())), "")
        own = "" if re.search(r" relais \d+$", b["name"]) else b["name"]
        out[b["key"]] = fam.get(b["key"]) or hit or most(b["inputs"]) or own
    for b in blocks:  # een timer staat bij de eerste lamp die hij blokkeert
        if b["kind"] == "timer":
            out[b["key"]] = next((out[l["key"]] for l in lamps if l.get("disable") == b["id"]), "")
    return out


def reset_layout(model: Model) -> None:
    """Alle ruimtes en plekken weg, voor een nieuwe indeling. De schakelaar-blokken en alle draden blijven."""
    s = copy.deepcopy(model.schema)
    s["pages"], s["blocks"] = [], {}
    model.set_schema(s)


def convert(model: Model, engine_snapshot, devices: list[dict] | None = None, areas: set[str] | None = None) -> dict:
    """Zet wat er nu is om naar blokken en pagina's. Raakt geen enkele schakeling, kan opnieuw (blokken die al staan
    blijven staan) en plaatst alleen wat iets doet: een los toestel zonder draden blijft in de lijst."""
    devices = devices or []
    switches, rooms = _new_switches(model, devices)
    added = len(switches) - len(model.schema["switches"])
    trial = copy.deepcopy(model.schema)
    trial["switches"] = switches
    model.set_schema(trial)
    snap = engine_snapshot()
    v = view(model, snap)
    blocks = v["blocks"]
    room_of = _rooms(blocks, rooms, (areas or set()) | {d.get("area") for d in devices if d.get("area")})

    # een gang van een extern apparaat is standaard een licht, ook als niemand hem bedient: dat is nog geen schakeling
    lamps = [b for b in blocks if b["kind"] == "lamp" and (b["source"] == "ha" or b["inputs"] or not is_virtual(b["node"]))]
    wired_in = {c["sw"] for b in lamps for c in b["inputs"]}
    wired_out = {c["sw"] for b in lamps for c in b["outputs"]}
    wanted_keys = {b["key"] for b in lamps} | wired_in | wired_out | {b["key"] for b in blocks if b["kind"] == "timer"}
    def wanted(b):
        return b["key"] in wanted_keys

    s = copy.deepcopy(model.schema)
    by_name = {p["name"].casefold(): p["id"] for p in s["pages"]}
    ids = {p["id"] for p in s["pages"]}
    def page_of(room: str) -> str:
        room = room or NO_ROOM
        if room.casefold() not in by_name:
            pid, n = _slug(room), 2
            while pid in ids:
                pid, n = f"{_slug(room)}_{n}", n + 1
            ids.add(pid)
            by_name[room.casefold()] = pid
            s["pages"].append({"id": pid, "name": room})
        return by_name[room.casefold()]

    heights = {b["key"]: _height(b) for b in blocks}
    bottom: dict[tuple[str, int], int] = {}
    for k, pos in s["blocks"].items():
        col = COL_LAMP if k[0] in "gl" else COL_IN
        bottom[(pos["page"], col)] = max(bottom.get((pos["page"], col), 0), pos["y"] + heights.get(k, 0) + GAP)
    placed = 0
    before = set(s["blocks"])
    todo = [b for b in blocks if b["key"] not in s["blocks"] and wanted(b)]
    # eerst de lampen; daarna elke schakelaar in de volgorde van de eerste lamp die hij bedient, zodat de draden kort blijven
    lamp_order = {b["key"]: i for i, b in enumerate(b for b in blocks if b["kind"] == "lamp")}
    def first_lamp(b):
        hits = [lamp_order[l["key"]] for l in blocks if l["kind"] == "lamp" for c in l["inputs"] + l["outputs"] if c["sw"] == b["key"]]
        return min(hits) if hits else len(lamp_order)
    for b in sorted(todo, key=lambda b: (b["kind"] != "lamp", first_lamp(b), b["name"].lower())):
        page = page_of(room_of.get(b["key"], ""))
        col = COL_LAMP if b["kind"] == "lamp" else COL_IN if b["key"] in wired_in or b["kind"] == "timer" else COL_OUT
        y = bottom.get((page, col), 30)
        s["blocks"][b["key"]] = {"page": page, "x": col, "y": y}
        bottom[(page, col)] = y + heights[b["key"]] + GAP
        placed += 1
    for p in s["pages"]:
        here = [k for k, v in s["blocks"].items() if v["page"] == p["id"]]
        if not here or before.intersection(here) or any(k[0] in "gl" for k in here):
            continue
        ys = [30] * GRID_COLS
        for k in here:  # steeds in de kortste kolom
            col = ys.index(min(ys))
            s["blocks"][k].update(x=GRID_X + col * GRID_STEP, y=ys[col])
            ys[col] += heights[k] + GAP
    # een lege ruimte (bv. alles later verplaatst) blijft staan: de gebruiker heeft hem misschien zelf gemaakt
    model.set_schema(s)
    return {"pages": len(s["pages"]), "placed": placed, "switches": added}
