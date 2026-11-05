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

from . import cards as _cards
from .link import split_ref
from .logic import MODULE_TYPES, SPRINKLER_INPUTS, disable_ref
from .model import MAX_SWITCH_GANGS, Model, ModelError, panel_relay_entity
from .proto import is_virtual

NO_ROOM = "Centraal"
# Hoogte en kolommen van de automatische indeling; de hoogtes zijn dezelfde als in static/schema.js
HEAD, ROW, FOOT, GAP = 42, 24, 10, 24
COL_IN, COL_LAMP, COL_OUT = 40, 460, 920
GRID_X, GRID_STEP, GRID_COLS = 20, 600, 2  # een pagina zonder lampen (Centraal): een raster, met naast elk blok plaats voor de verwijzingen
NOT_A_GANG = re.compile(r"(^|_)(led|leds|all|all_relays|status|backlight|buzzer)(_|$)")  # zoals in de import van app.js
TUYA_RELAY = re.compile(r"^switch(_\d+)?$")
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


def _tuya_owners(snap: dict) -> dict[str, tuple[str, int]]:
    return {tuya_entity(t["id"], p["dp"]): (f"y:{t['id']}", i) for t in snap.get("tuya", []) for i, p in enumerate(_tuya_relays(t))}


def _gang_owners(model: Model, snap: dict) -> dict[str, tuple[str, int]]:
    """Entiteit -> (blok, gang). Een extern apparaat of Tuya-toestel gaat voor: zo verhuist een gang vanzelf mee als je een toestel importeert."""
    own: dict[str, tuple[str, int]] = {}
    for vid, gangs in model.virtual.items():
        for i, g in enumerate(gangs):
            own.setdefault(g["entity"], (f"n{vid}", i))
    for e, o in _tuya_owners(snap).items():
        own.setdefault(e, o)
    for sid, s in model.schema["switches"].items():
        for i, g in enumerate(s["gangs"]):
            own.setdefault(g["entity"], (f"d:{sid}", i))
    for pid in model.panels:
        for i in range(2):
            own.setdefault(panel_relay_entity(pid, i + 1), (f"p:{pid}", i))
    for pid, p in ((snap.get("links") or {}).get("remote") or {}).items():  # een gang van een andere installatie: het lid is het blok zelf
        for obj, o in p["objects"].items():
            if o.get("kind") == "gang":
                own.setdefault(f"r:{pid}:{obj}", (f"r:{pid}:{obj}", 0))
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
                    "disable": g.get("disable"), "blocked": g.get("blocked", False), "control": g.get("control"), "held": g.get("held")})
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


def _device_label(snap: dict, dev: str, dp: str) -> str:
    """"Toestel · Datapunt" voor een module of routine: `<id>` hier of `r:..:y:<id>` bij een andere installatie."""
    if not dev:
        return "nog geen toestel"
    return _point_label(snap, dev if dev.startswith("r:") else f"y:{dev}", dp)[1] if dp else (dev if dev.startswith("r:") else dev)


def _module_blocks(snap: dict) -> list[dict]:
    out = []
    for m in snap.get("modules", []):
        t = MODULE_TYPES[m["type"]]
        detail = (_device_label(snap, m.get("device", ""), m.get("dp", "")) if m["type"] == "dp"
                  else f"{'>' if m['type'] == 'above' else '<'} {m['value']:g}" if m["type"] in ("above", "below") else "")
        out.append({"key": f"m:{m['id']}", "kind": "module", "source": "module", "type": m["type"], "id": m["id"], "name": m["name"], "tech": t["label"],
                    "alias": "", "room": "", "online": True, "value": m["out"], "out": t["out"], "param": m.get("value"), "device": m.get("device", ""),
                    "dp": m.get("dp", ""), "detail": detail,
                    "ports": [{"name": n, "type": kind, "label": label, "ref": m["inputs"].get(n)} for n, kind, label in t["inputs"]]})
    return out


def _action_blocks(snap: dict) -> list[dict]:
    """Tuya instellen: links de ingang (een 0/1), op het blok wat ze zet en wat ze laatst deed."""
    return [{**{k: a[k] for k in ("id", "name", "enabled", "device", "dp", "on_value", "off_value", "interval", "status")},
             "key": f"a:{a['id']}", "kind": "action", "source": "action", "tech": "Tuya instellen", "alias": "", "room": "", "online": True,
             "target": _device_label(snap, a["device"], a["dp"]), "rows": 2,
             "ports": [{"name": "trigger", "type": "bool", "label": "ingang", "ref": a["inputs"].get("trigger")}]}
            for a in snap.get("actions", [])]


def _sprinkler_blocks(snap: dict) -> list[dict]:
    return [{**{k: s[k] for k in ("id", "name", "enabled", "start", "cycles", "wait", "refresh", "days", "zones", "status")},
             "key": f"s:{s['id']}", "kind": "sprinkler", "source": "sprinkler", "tech": "Sprinkler", "alias": "", "room": "", "online": True,
             "ports": [{"name": n, "type": kind, "label": label, "ref": s["inputs"].get(n)} for n, kind, label in SPRINKLER_INPUTS]}
            for s in snap.get("sprinklers", [])]


def _panel_blocks(model: Model, snap: dict) -> list[dict]:
    """Bovenaan de twee relais (zoals een schakelaar), daaronder de kaarten in volgorde: links een ingang per kaart en één vrije."""
    out = []
    for p in snap.get("panels", []):
        m = model.panels.get(p["id"]) or {}
        cards = [{"key": f"c:{c}", "id": c, "name": model.cards[c]["name"], "type": model.cards[c]["type"]} for c in m.get("cards", []) if c in model.cards]
        out.append({**{k: p.get(k) for k in ("id", "name", "host", "online", "error", "greeted", "last_seen", "temperature", "rssi")},
                    **{k: m.get(k) for k in ("locale", "timeout", "bright", "dim")}, "notes": m.get("notes", []), "cards": cards,
                    "key": f"p:{p['id']}", "kind": "panel", "source": "panel", "tech": "NSPanel", "alias": "", "room": "",
                    "gangs": [{"i": i, "label": f"Gang {i + 1}", "entity": panel_relay_entity(p["id"], i + 1), "touch": True, "relay": True, "on": p["relays"][i]} for i in range(2)]})
    return out


def _remote_obj(snap: dict, ref: str) -> tuple[dict, dict] | None:
    """(installatie, object) uit de status van de koppelingen, voor `r:<installatie>:<object>`."""
    hit = split_ref(ref or "")
    p = (snap.get("links") or {}).get("remote", {}).get(hit[0]) if hit else None
    o = p["objects"].get(hit[1]) if p else None
    return (p, o) if o else None


def _point_label(snap: dict, ref: str, dp: str) -> tuple[str | None, str]:
    """(blok, "Toestel · Datapunt") voor een waarde op een kaart: `y:<id>` hier of `r:..:y:<id>` bij een andere installatie."""
    if ref.startswith("y:"):
        t = next((x for x in snap.get("tuya", []) if x["id"] == ref[2:]), None)
        if t is None:
            return None, "toestel weg"
        p = next((x for x in t["points"] if x["dp"] == dp), None)
        return ref, f"{t['name']} · {p['name'] if p else f'datapunt {dp}'}"
    hit = _remote_obj(snap, ref)
    if not hit:
        return None, "toestel van een andere installatie, weg"
    p = next((x for x in hit[1].get("points") or [] if str(x.get("dp")) == dp), None)
    return ref, f"{hit[1].get('name')} · {p['name'] if p else f'datapunt {dp}'} ({hit[0]['name']})"


def _remote_blocks(snap: dict) -> list[dict]:
    """Wat andere installaties delen: alleen-lezen, te bedienen, en aan een kaart te hangen. Instellen gebeurt bij de eigenaar."""
    out = []
    for pid, p in ((snap.get("links") or {}).get("remote") or {}).items():
        for obj, o in sorted(p["objects"].items()):
            pts = [x for x in o.get("points") or [] if not x.get("hide") and not x.get("diag")]
            kind, key = o.get("kind"), f"r:{pid}:{obj}"
            shown = [x for x in pts if x.get("rw")], [x for x in pts if not x.get("rw")]  # zoals een Tuya-blok van hier: links instellen, rechts meldingen
            # een gang: touch en relais zoals een schakelaar, met de verwijzing zelf als entiteit (een lid van een kring)
            gangs = [{"i": 0, "label": o.get("name") or obj, "entity": key, "touch": True, "relay": True, "on": o.get("on")}] if kind == "gang" else []
            out.append({"key": key, "kind": "remote", "source": "remote", "sub": kind, "peer": pid, "peerName": p["name"], "obj": obj,
                        "name": o.get("name") or obj, "tech": f"{p['name']} · {obj}", "alias": "", "room": "", "online": bool(p["online"] and o.get("online")),
                        "linked": p["online"], "on": o.get("on"), "held": bool(o.get("held")), "busy": bool(o.get("busy")), "points": pts, "gangs": gangs,
                        "pin": shown[0], "pout": shown[1], "rows": 1 if kind in ("lamp", "gang") else max(len(shown[0]), len(shown[1]), 1)})
    return out


def _card_item(model: Model, it: dict, by_key: dict, snap: dict) -> dict:
    """Een knop van een kaart, met wat het blok erover laat zien."""
    ref, ent = it.get("ref"), it.get("entity")
    target = label = None
    if it.get("dp") and ref:
        target, label = _point_label(snap, ref, it["dp"])
    elif ref and ref.startswith("c:"):
        c = model.cards.get(ref[2:])
        target, label = ref, (f"→ {c['name']}" if c else "subpagina weg")
    elif ref and ref.startswith("r:"):
        hit = _remote_obj(snap, ref)
        target, label = (ref if hit else None), (f"{hit[1].get('name')} ({hit[0]['name']})" if hit else "lamp van een andere installatie, weg")
    elif ref:
        name = _cards.lamp_name(model, ref)
        target, label = (ref if ref in by_key else None), name or "lamp weg"
    elif ent:
        label = ent
    return {**it, "target": target, "label": it.get("name") or label or "leeg"}


def _card_blocks(model: Model, snap: dict, lamps: set[str]) -> list[dict]:
    out = []
    for cid, c in model.cards.items():
        b = {**c, "key": f"c:{cid}", "id": cid, "kind": "card", "source": "card", "tech": f"Kaart · {_cards.TYPES[c['type']]}", "alias": "", "room": "",
             "online": True, "users": _cards.users(model, cid)}
        if c["type"] == "grid":
            b["items"] = [_card_item(model, it, lamps, snap) for it in c["items"]]
            b["rows"] = len(c["items"]) + (1 if len(c["items"]) < _cards.MAX_ITEMS[c["layout"]] else 0)
        elif c["type"] == "charger":  # links drie ingangen, rechts start/stop; `inputs` heet hier `ins` (een lamp-blok heeft al `inputs`)
            b.pop("inputs", None)
            b["ins"] = [{"name": n, "label": lbl, "ref": c["inputs"].get(n), "src": _device_label(snap, *_cards.value_ref(c["inputs"][n])) if c["inputs"].get(n) else None}
                        for n, lbl in _cards.CHARGER_INPUTS.items()]
            tg = c["target"]
            b["target_label"] = _device_label(snap, tg["device"], tg["dp"]) if tg["device"] and tg["dp"] else None
            b["rows"] = 4
        elif c["type"] == "power":
            b["rows"] = max(len(c["items"]), 1)
            b["labels"] = [(_point_label(snap, it["tuya"], it["dp"])[1] if it.get("tuya") else it.get("name") or it.get("entity") or "leeg") for it in c["items"]]
        elif c["type"] == "airco":  # de thermostaatpagina van een airco-blok (w:<id>); `device` is die airco, of None
            a = next((x for x in snap.get("airco", []) if x["id"] == c["airco"]), None)
            b["device"] = {"name": a["name"], "online": a["online"]} if a else None
            b["rows"] = 2
        elif c["type"] == "saver":
            b["rows"] = 3
        else:
            b["rows"] = 2
        out.append(b)
    return out


def _screen_blocks(model: Model, snap: dict) -> list[dict]:
    """Zoals een paneel zonder relais: de kaarten in volgorde, links een ingang per kaart en één vrije."""
    out = []
    for s in snap.get("screens", []):
        m = model.screens.get(s["id"]) or {}
        cards = [{"key": f"c:{c}", "id": c, "name": model.cards[c]["name"], "type": model.cards[c]["type"]} for c in m.get("cards", []) if c in model.cards]
        out.append({**{k: s.get(k) for k in ("id", "name", "plate", "timeout", "online", "phase", "error", "missing", "uptime", "plates")},
                    "locale": m.get("locale", "nl"), "cards": cards, "key": f"h:{s['id']}", "kind": "screen", "source": "screen", "tech": "openHASP",
                    "alias": "", "room": "", "gangs": []})
    return out


def _tuya_relays(t: dict) -> list[dict]:
    """De relais van een Tuya-toestel: `switch` of `switch_<n>`, schrijfbaar aan/uit. Niet `switch_backlight` en dergelijke."""
    return [p for p in t["points"] if p["rw"] and p["type"] == "bool" and not p["hide"] and TUYA_RELAY.match(p.get("code") or "")]


def tuya_entity(did: str, dp: str) -> str:
    return f"switch.kk_tuya_{did}_{dp}"  # zoals tuyahub.discovery hem aanmaakt


def _tuya_blocks(snap: dict) -> list[dict]:
    """Elk toestel zijn eigen poorten, uit zijn datamodel. Bovenaan de relais, als gangen met een touch en een relais zoals bij een
    schakelaar; daaronder links wat je kunt instellen, rechts wat het alleen meldt. Diagnose staat alleen in de zijbalk."""
    out = []
    for t in snap.get("tuya", []):
        relays = _tuya_relays(t)
        shown = [p for p in t["points"] if not p["hide"] and not p["diag"] and p not in relays]
        pin = [p for p in shown if p["rw"]]
        pout = [p for p in shown if not p["rw"]]
        gangs = [{"i": i, "label": p["name"], "entity": tuya_entity(t["id"], p["dp"]), "touch": True, "relay": True, "on": p["value"] if isinstance(p["value"], bool) else None}
                 for i, p in enumerate(relays)]
        rest = max(len(pin), len(pout), 0 if gangs else 1)
        gangs += [{"i": len(gangs) + i, "label": "", "entity": None, "touch": False, "relay": False, "on": None} for i in range(rest)]
        note = "" if relays or shown else "Nog geen waarden" if t["configured"] else "Nog niet ingesteld"
        out.append({**t, "key": f"y:{t['id']}", "kind": "tuya", "source": "tuya", "tech": "Tuya", "alias": "", "room": "", "pin": pin, "pout": pout, "note": note,
                    "nrelay": len(relays), "gangs": gangs})
    return out


def _airco_blocks(snap: dict) -> list[dict]:
    """Een airco: links wat haar stuurt (aan, temperatuur, blokkeren), rechts wat ze meldt (aan, kamer, ingesteld)."""
    from .greehub import INPUTS, OUTPUTS
    # `inputs`/`outputs` heten hier `ports`/`outs`: een lamp-blok gebruikt die namen al voor zijn draden
    return [{**{k: v for k, v in a.items() if k not in ("inputs", "outputs")}, "key": f"w:{a['id']}", "kind": "airco", "source": "airco", "tech": "Airco · Gree", "alias": "", "room": "", "rows": len(INPUTS),
             "ports": [{"name": n, "type": kind, "label": label, "ref": a["inputs"].get(n)} for n, kind, label in INPUTS],
             "outs": [{"name": n, "type": kind, "label": label, "ref": f"w:{a['id']}:{n}", "value": a["outputs"].get(n)} for n, kind, label in OUTPUTS]}
            for a in snap.get("airco", [])]


def _solar_blocks(snap: dict) -> list[dict]:
    """Een omvormer: alleen uitgangen rechts. `outs` zijn de getoonde (de rijen van het blok), `all` alle, voor de keuzelijsten van modules."""
    from .solarhub import OUTPUTS
    def out(a, n, kind, label, unit, group):
        return {"name": n, "type": kind, "label": label, "unit": unit, "group": group, "ref": f"o:{a['id']}:{n}", "value": a["outputs"].get(n)}
    res = []
    for a in snap.get("solar", []):
        every = [out(a, *o) for o in OUTPUTS]
        shown = [o for o in every if o["name"] in a["shown"]]
        res.append({"id": a["id"], "name": a["name"], "host": a["host"], "serial": a["serial"], "online": a["online"], "error": a["error"], "values": a["values"],
                    "shown": a["shown"], "key": f"o:{a['id']}", "kind": "solar", "source": "solar", "tech": "Omvormer · Solarman", "alias": "", "room": "",
                    "rows": max(len(shown), 1), "ports": [], "outs": shown, "all": every})
    return res


def view(model: Model, snap: dict) -> dict:
    """Alles wat de pagina nodig heeft om het schema te tekenen."""
    states = {m["entity"]: m["on"] for g in snap["groups"] for m in g["members"]}
    owners = _gang_owners(model, snap)
    lamps = _lamp_blocks(model, snap, owners)
    blocks = (_switch_blocks(model, snap, owners, states) + lamps + _timer_blocks(snap) + _module_blocks(snap) + _sprinkler_blocks(snap) + _panel_blocks(model, snap)
              + _screen_blocks(model, snap) + _tuya_blocks(snap) + _airco_blocks(snap) + _solar_blocks(snap) + _remote_blocks(snap) + _action_blocks(snap))
    blocks += _card_blocks(model, snap, {b["key"] for b in lamps})
    names = {b["key"]: b for b in blocks if b["kind"] in ("switch", "panel", "tuya") or (b["kind"] == "remote" and b["sub"] == "gang")}
    for b in blocks:
        for c in b.get("inputs", []) + b.get("outputs", []):
            sw = names.get(c["sw"])
            if sw is None:
                c["sw"] = c["gang"] = None
            elif c["gang"] < len(sw["gangs"]):
                c["label"] = sw["gangs"][c["gang"]]["label"]
                c["swName"] = sw["peerName"] if sw["kind"] == "remote" else sw["name"]  # een gang van een andere installatie: "Atelier · Touch poort · links"
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
        if str(ref["entity"]).startswith("r:"):
            raise ModelError("Een gang van een andere installatie kan alleen op een lamp via Home Assistant (een kring), niet op een licht op het netwerk.")
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
    """De disable-ingang van een lamp: een timer-id, een module (`m:<id>`) of None. Een licht op het netwerk beslist op de node zelf en kan dit nog niet."""
    if not lamp.startswith("g:"):
        raise ModelError("Disable kan voorlopig alleen op een lamp via Home Assistant: een licht op het netwerk beslist op de node zelf.")
    if lamp[2:] not in model.groups:
        raise ModelError("onbekende lamp")
    model.set_group(lamp[2:], disable=timer)


def set_control(model: Model, lamp: str, source) -> None:
    """De aan/uit-ingang van een lamp: een timer-id, een module (`m:<id>`) of None. Net als disable alleen via Home Assistant."""
    if not lamp.startswith("g:"):
        raise ModelError("De aan/uit-ingang kan voorlopig alleen op een lamp via Home Assistant.")
    if lamp[2:] not in model.groups:
        raise ModelError("onbekende lamp")
    model.set_group(lamp[2:], control=source)


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


def _new_switches(model: Model, devices: list[dict], snap: dict) -> tuple[dict, dict]:
    """Maakt een schakelaar-blok voor elke entiteit van een kring die er nog geen heeft. Uit het HA-apparatenregister,
    of (zonder register) op de naam van de entiteit. Geeft ook terug in welke ruimte elk nieuw blok hoort."""
    switches = copy.deepcopy(model.schema["switches"])
    claimed = ({g["entity"] for gs in model.virtual.values() for g in gs} | {g["entity"] for s in switches.values() for g in s["gangs"]}
               | set(_tuya_owners(snap)))
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
    if b["kind"] == "sprinkler":
        return HEAD + max(len(b["ports"]), len(b["zones"])) * ROW + FOOT
    if b["kind"] == "module":
        return HEAD + max(len(b["ports"]), 1) * ROW + FOOT
    if b["kind"] in ("card", "remote", "action", "airco", "solar"):
        return HEAD + b["rows"] * ROW + FOOT
    if b["kind"] in ("panel", "screen"):
        return HEAD + (len(b["gangs"]) + len(b["cards"]) + 1) * ROW + FOOT
    rows = len(b["gangs"]) if b["kind"] in ("switch", "screen", "tuya") else max(len(b["inputs"]), len(b["outputs"])) + (3 if b["source"] == "ha" else 1)
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
    by_key = {b["key"]: b for b in blocks}
    def upstream(key: str, room: str) -> None:  # een timer of module staat bij de eerste lamp die ze (via via) blokkeert
        b = by_key.get(key)
        if b is None or key in out:
            return
        out[key] = room
        for p in b.get("ports", []):
            if p["ref"]:
                upstream(p["ref"], room)
    for l in lamps:
        for src in (l.get("control"), l.get("disable")):
            if src:
                upstream(disable_ref(src), out[l["key"]])
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
    switches, rooms = _new_switches(model, devices, engine_snapshot())
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
    wanted_keys = {b["key"] for b in lamps} | wired_in | wired_out | {b["key"] for b in blocks if b["kind"] in ("timer", "module", "sprinkler", "panel", "screen", "tuya", "airco", "solar", "card", "action")}
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
        col = COL_LAMP if b["kind"] == "lamp" else COL_IN if b["key"] in wired_in or b["kind"] in ("timer", "module", "sprinkler", "panel", "screen", "tuya", "airco", "solar", "card", "action") else COL_OUT
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
