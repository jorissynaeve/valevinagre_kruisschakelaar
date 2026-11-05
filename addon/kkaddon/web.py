"""Web-UI en API (HA-ingress, poort 8099). Alleen de HA-ingress-proxy (172.30.32.2) mag erbij, tenzij dev_mode."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path

from aiohttp import WSMsgType, web

from . import __version__, appdaemon_import, inventory, schema, tasmota
from .engine import Engine
from .link import LinkError
from .model import ENTITY_RE, Model, ModelError
from .settings import Settings, key_header

log = logging.getLogger("kk.web")

STATIC = Path(__file__).parent / "static"
CTX = web.AppKey("ctx", object)
INGRESS_PROXY = {"172.30.32.2", "127.0.0.1", "::1"}
# Events die de pagina moet kennen; de rest (bv. losse commando-statussen) is ruis.
PUSH = {"btn", "light", "node", "cfg", "learn", "cmd", "sync", "grp", "model", "sprinkler", "panel", "screen", "tuya", "airco", "solar", "link", "action"}


class Ctx:
    """Alles wat de handlers nodig hebben."""

    def __init__(self, settings: Settings, engine: Engine, transport=None, mqtt=None, ha=None, panels=None, tuya=None, links=None, airco=None, solar=None) -> None:
        self.settings, self.engine, self.model = settings, engine, engine.model
        self.transport, self.mqtt, self.ha, self.panels, self.tuya, self.links = transport, mqtt, ha, panels, tuya, links
        self.airco = airco
        self.solar = solar  # omvormers (solarhub.py)
        self.clients: set[web.WebSocketResponse] = set()
        engine.subscribe(self._on_event)

    def _on_event(self, e: dict) -> None:
        if e["t"] not in PUSH:
            return
        msg = json.dumps({"event": e})
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            asyncio.get_running_loop().create_task(self._send(ws, msg))

    async def _send(self, ws: web.WebSocketResponse, msg: str) -> None:
        try:
            await ws.send_str(msg)
        except ConnectionError:
            self.clients.discard(ws)


@web.middleware
async def guard(request: web.Request, handler):
    ctx: Ctx = request.app[CTX]
    if not ctx.settings.dev_mode and request.remote not in INGRESS_PROXY:
        raise web.HTTPForbidden(text="alleen via Home Assistant (ingress)")
    try:
        return await handler(request)
    except ModelError as e:
        return web.json_response({"error": str(e)}, status=400)
    except (ValueError, KeyError, TypeError) as e:
        return web.json_response({"error": f"ongeldige aanvraag: {e}"}, status=400)


async def _no_cache(request: web.Request, response: web.StreamResponse) -> None:
    """Na een update van de add-on moet de browser de nieuwe pagina en scripts halen, niet een oude kopie (ingress cachet graag)."""
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


def _ids(request: web.Request, *names: str) -> list[int]:
    try:
        return [int(request.match_info[n]) for n in names]
    except ValueError:
        raise ModelError("ongeldig nummer in het adres")


async def _body(request: web.Request) -> dict:
    try:
        b = await request.json()
    except json.JSONDecodeError:
        raise ModelError("geen geldige JSON")
    if not isinstance(b, dict):
        raise ModelError("JSON-object verwacht")
    return b


def build_state(ctx: Ctx) -> dict:
    snap = ctx.engine.snapshot()
    snap["version"] = __version__
    snap["log"] = list(ctx.engine.log)[-150:]
    snap["peers"] = ctx.transport.peers_info() if ctx.transport else []
    snap["mqtt"] = {"connected": bool(ctx.mqtt and ctx.mqtt.connected), "enabled": bool(ctx.mqtt and ctx.mqtt.enabled), "error": ctx.mqtt.last_error if ctx.mqtt else ""}
    snap["ha"] = {"enabled": bool(ctx.ha and ctx.ha.enabled), "connected": bool(ctx.ha and ctx.ha.connected), "error": ctx.ha.last_error if ctx.ha else ""}
    snap["plan"] = ctx.model.plan
    snap["names"] = {str(k): v.get("name", "") for k, v in ctx.model.nodes.items()}
    snap["schema"] = schema.view(ctx.model, snap)
    return snap


async def state(request: web.Request) -> web.Response:
    return web.json_response(build_state(request.app[CTX]))


async def light_cmd(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    node, relay = _ids(request, "node", "relay")
    op = (await _body(request)).get("op")
    if op not in ("on", "off", "toggle"):
        raise ModelError("op moet on, off of toggle zijn")
    ok = ctx.engine.toggle_light(node, relay) if op == "toggle" else ctx.engine.command_light(node, relay, op == "on")
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def light_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    node, relay = _ids(request, "node", "relay")
    b = await _body(request)
    ctx.model.set_light(node, relay, name=b.get("name"), enabled=b.get("enabled"))
    if "triggers" in b:
        ctx.engine.set_triggers(node, relay, b["triggers"])
    return web.json_response({"ok": True})


async def light_delete(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    node, relay = _ids(request, "node", "relay")
    old = ctx.model.lights.get((node, relay))
    affected = {node} | {t.node for t in old.triggers} if old else set()
    ok = ctx.model.remove_light(node, relay)
    for n in sorted(affected):
        ctx.engine.sync_node(n)
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def node_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    (node,) = _ids(request, "node")
    b = await _body(request)
    ctx.model.set_node(node, name=b.get("name"), room=b.get("room"), adopted=b.get("adopted"))
    return web.json_response({"ok": True})


async def node_identify(request: web.Request) -> web.Response:
    (node,) = _ids(request, "node")
    request.app[CTX].engine.identify(node)
    return web.json_response({"ok": True})


async def node_sync(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    (node,) = _ids(request, "node")
    b = await _body(request)
    ctx.model.adopt(node)
    started = ctx.engine.sync_node(node, full=bool(b.get("full", True)))
    return web.json_response({"ok": True, "started": started})


async def follow_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    node, ind = _ids(request, "node", "ind")
    light = (await _body(request)).get("light")
    if isinstance(light, dict):
        light = (int(light["node"]), int(light["relay"]))
    elif light is not None and light != "auto":
        raise ModelError('light moet null, "auto" of {node, relay} zijn')
    ctx.model.set_follow(node, ind, light)
    ctx.engine.sync_node(node)
    return web.json_response({"ok": True})


async def learn_start(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    target = (int(b["node"]), int(b["relay"])) if "node" in b else None
    ctx.engine.learn_start(target)
    return web.json_response({"ok": True})


async def learn_stop(request: web.Request) -> web.Response:
    request.app[CTX].engine.learn_stop()
    return web.json_response({"ok": True})


async def learn_save(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    ctx.engine.learn_save((await _body(request)).get("sources", []))
    return web.json_response({"ok": True})


async def ha_entities(request: web.Request) -> web.Response:
    ha = request.app[CTX].ha
    return web.json_response({"connected": bool(ha and ha.connected), "entities": await ha.entities() if ha else []})


async def groups_parse(request: web.Request) -> web.Response:
    """Voorbeeld van een import: wat staat er in de geplakte YAML, wat bestaat al, wat kent HA niet."""
    ctx: Ctx = request.app[CTX]
    res = appdaemon_import.parse(str((await _body(request)).get("yaml") or ""))
    known = {e["entity_id"] for e in await ctx.ha.entities()} if ctx.ha and ctx.ha.connected else None
    for g in res["groups"]:
        g["exists"] = g["id"] in ctx.model.groups
        g["missing"] = None if known is None else [e for e in g["entities"] if e not in known]
    return web.json_response({"ha": known is not None, **res})


async def groups_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    ids = ctx.model.set_groups((await _body(request)).get("groups"))
    return web.json_response({"ok": True, "ids": ids})


async def group_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    gid = request.match_info["gid"]
    b = await _body(request)
    ctx.model.set_group(gid, name=b.get("name"), entities=b.get("entities"), enabled=b.get("enabled"), lamp=b.get("lamp"))
    return web.json_response({"ok": True})


async def group_cmd(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    op = b.get("op", "toggle")
    if op not in ("on", "off", "toggle"):
        raise ModelError("op moet on, off of toggle zijn")
    ent = b.get("entity")
    if ent is not None and not isinstance(ent, str):
        raise ModelError("entity moet een tekst zijn")
    if ctx.engine.groups.held(request.match_info["gid"]) is not None:
        raise ModelError("Deze lamp wordt vastgehouden door zijn aan/uit-ingang.")
    ok = ctx.engine.groups.command(request.match_info["gid"], None if op == "toggle" else op == "on", ent)
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def group_delete(request: web.Request) -> web.Response:
    ok = request.app[CTX].model.remove_group(request.match_info["gid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def groups_all_off(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "disabled": request.app[CTX].model.disable_all_groups()})


async def plan_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    ctx.model.set_plan(await _body(request))
    return web.json_response({"ok": True})


async def schema_convert(request: web.Request) -> web.Response:
    """Het huidige omzetten naar blokken en pagina's. Zonder HA-register wordt op entiteit-id gegroepeerd en zijn er geen ruimtes."""
    ctx: Ctx = request.app[CTX]
    if (await _body(request)).get("reset"):
        schema.reset_layout(ctx.model)
    r = await ctx.ha.devices() if ctx.ha else {"registry": False, "devices": []}
    res = schema.convert(ctx.model, ctx.engine.snapshot, r["devices"])
    return web.json_response({"ok": True, "registry": r["registry"], **res})


async def schema_place(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    schema.place(ctx.model, ctx.engine.snapshot(), str(b.get("key")), b.get("page"), b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True})


async def schema_disable(request: web.Request) -> web.Response:
    b = await _body(request)
    schema.set_disable(request.app[CTX].model, str(b.get("lamp")), b.get("timer"))
    return web.json_response({"ok": True})


async def schema_control(request: web.Request) -> web.Response:
    b = await _body(request)
    schema.set_control(request.app[CTX].model, str(b.get("lamp")), b.get("source"))
    return web.json_response({"ok": True})


async def timer_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    tid = ctx.model.save_timer(None, b.get("name"), b.get("days"))
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"t:{tid}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "key": f"t:{tid}"})


async def timer_put(request: web.Request) -> web.Response:
    b = await _body(request)
    request.app[CTX].model.save_timer(request.match_info["tid"], b.get("name"), b.get("days"))
    return web.json_response({"ok": True})


async def timer_delete(request: web.Request) -> web.Response:
    ok = request.app[CTX].model.remove_timer(request.match_info["tid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def module_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    mid = ctx.model.add_module(b.get("type"), b.get("name"), b.get("value"))
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"m:{mid}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "key": f"m:{mid}"})


async def module_put(request: web.Request) -> web.Response:
    b = await _body(request)
    request.app[CTX].model.set_module(request.match_info["mid"], b.get("name"), b.get("value"), b.get("inputs"), **{k: b[k] for k in ("device", "dp") if k in b})
    return web.json_response({"ok": True})


async def module_delete(request: web.Request) -> web.Response:
    ok = request.app[CTX].model.remove_module(request.match_info["mid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def sprinkler_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    sid = ctx.model.add_sprinkler(b.get("name"), b.get("zones", 3))
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"s:{sid}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "key": f"s:{sid}"})


async def action_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    aid = ctx.model.add_action(b.get("name"))
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"a:{aid}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "key": f"a:{aid}"})


async def action_put(request: web.Request) -> web.Response:
    request.app[CTX].model.set_action(request.match_info["aid"], **await _body(request))
    return web.json_response({"ok": True})


async def action_delete(request: web.Request) -> web.Response:
    ok = request.app[CTX].model.remove_action(request.match_info["aid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def sprinkler_put(request: web.Request) -> web.Response:
    request.app[CTX].model.set_sprinkler(request.match_info["sid"], **await _body(request))
    return web.json_response({"ok": True})


async def sprinkler_delete(request: web.Request) -> web.Response:
    ok = request.app[CTX].model.remove_sprinkler(request.match_info["sid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


def _panels(request: web.Request):
    hub = request.app[CTX].panels
    if hub is None:
        raise ModelError("panelen zijn niet ingeschakeld")
    return hub


async def panel_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    pid = ctx.model.add_panel(b.get("name"), b.get("host") or "")
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"p:{pid}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "id": pid, "key": f"p:{pid}"})


async def panel_yaml(request: web.Request) -> web.Response:
    """Een YAML van nspanel-lovelace-ui omzetten naar kaarten aan dit paneel. De kaarten komen op de ruimte van het paneel."""
    ctx: Ctx = request.app[CTX]
    pid = request.match_info["pid"]
    before = set(ctx.model.cards)
    notes = ctx.model.import_panel_yaml(pid, (await _body(request)).get("yaml"))
    new = [c for c in ctx.model.cards if c not in before]
    ctx.model.place_cards_near(pid, new)
    ctx.model.save()
    return web.json_response({"ok": True, "notes": notes, "cards": new})


def _links(request: web.Request):
    hub = request.app[CTX].links
    if hub is None:
        raise ModelError("koppelingen zijn niet beschikbaar")
    return hub


async def link_put(request: web.Request) -> web.Response:
    """Naam van deze installatie en de koppelsleutel."""
    _links(request).configure(await _body(request))
    return web.json_response({"ok": True})


async def link_peer_add(request: web.Request) -> web.Response:
    b = await _body(request)
    _links(request).add_peer(b.get("host"), b.get("port", 47490))
    return web.json_response({"ok": True})


async def link_peer_remove(request: web.Request) -> web.Response:
    b = await _body(request)
    ok = _links(request).remove_peer(b.get("host"), b.get("port"))
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def link_use(request: web.Request) -> web.Response:
    """Een toestel van een andere installatie wel of niet gebruiken: {peer, obj, use}."""
    b = await _body(request)
    _links(request).set_use(str(b.get("peer")), b.get("obj"), bool(b.get("use")))
    return web.json_response({"ok": True})


async def link_forget(request: web.Request) -> web.Response:
    ok = _links(request).forget(str((await _body(request)).get("peer")))
    if not ok:
        raise ModelError("alleen een installatie die niet verbonden is kan vergeten worden")
    return web.json_response({"ok": True})


async def link_cmd(request: web.Request) -> web.Response:
    """Een toestel van een andere installatie bedienen: {peer, obj, op} of {peer, obj, dp, value}. Wacht op het antwoord van de eigenaar."""
    b = await _body(request)
    cmd = {"op": b["op"]} if "op" in b else {"dp": str(b.get("dp")), "value": str(b.get("value"))}
    try:
        await _links(request).command(str(b.get("peer")), str(b.get("obj")), **cmd)
    except LinkError as e:
        raise ModelError(str(e)) from e
    return web.json_response({"ok": True})


async def card_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    cid = ctx.model.add_card(b.get("type"), b.get("name"))
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"c:{cid}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "id": cid, "key": f"c:{cid}"})


async def card_put(request: web.Request) -> web.Response:
    request.app[CTX].model.set_card(request.match_info["cid"], **await _body(request))
    return web.json_response({"ok": True})


async def card_delete(request: web.Request) -> web.Response:
    ok = request.app[CTX].model.remove_card(request.match_info["cid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def panel_put(request: web.Request) -> web.Response:
    request.app[CTX].model.set_panel(request.match_info["pid"], **await _body(request))
    return web.json_response({"ok": True})


async def panel_delete(request: web.Request) -> web.Response:
    ok = request.app[CTX].model.remove_panel(request.match_info["pid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def panel_relay(request: web.Request) -> web.Response:
    b = await _body(request)
    if not _panels(request).set_relay(request.match_info["pid"], int(request.match_info["n"]), bool(b.get("on"))):
        raise ModelError("onbekend paneel of relais, of het paneel heeft nog geen IP-adres")
    return web.json_response({"ok": True})


def _script_text(request: web.Request, host: str) -> str:
    """Het Berry-script voor de panel, met het adres van deze add-on erin."""
    if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
        raise ModelError("geef het IP-adres van Home Assistant mee (?hub=192.168.x.y)")
    text = (Path(__file__).parent / "berry" / "huisconfig.be").read_text(encoding="utf-8")
    return text.replace("@HUB_HOST@", host).replace("@HUB_PORT@", str(_panels(request).port)).replace("@PANEL_PORT@", str(_panels(request).panel_port))


async def panel_install(request: web.Request) -> web.Response:
    """Het script op de panel zetten via de webserver van Tasmota. Het wachtwoord van die webserver wordt niet bewaard."""
    pid = request.match_info["pid"]
    model = request.app[CTX].model
    p = model.panels.get(pid)
    if p is None:
        raise ModelError("onbekend paneel")
    if not p["host"]:
        raise ModelError("vul eerst het IP-adres van de panel in")
    b = await _body(request)
    hub = str(b.get("hub") or p.get("hub") or "").strip()
    if hub != p.get("hub"):
        model.set_panel(pid, hub=hub)  # onthouden voor de volgende keer
    try:
        done = await tasmota.install(p["host"], _script_text(request, hub), str(b.get("password") or ""))
    except tasmota.TasmotaError as e:
        raise ModelError(str(e)) from e
    return web.json_response({"ok": True, "done": done})


async def panel_script(request: web.Request) -> web.Response:
    text = _script_text(request, request.query.get("hub", "").strip())
    return web.Response(text=text, content_type="text/plain", headers={"Content-Disposition": 'attachment; filename="huisconfig.be"'})


def _tuya(request: web.Request):
    hub = request.app[CTX].tuya
    if hub is None:
        raise ModelError("Tuya is niet beschikbaar")
    return hub


async def tuya_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    did = _tuya(request).add(b)
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"y:{did}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "id": did, "key": f"y:{did}"})


async def tuya_put(request: web.Request) -> web.Response:
    hub, did, b = _tuya(request), request.match_info["did"], await _body(request)
    old = hub.devices.get(did, {}).get("device_id")
    hub.update(did, b)
    if b.get("device_id") and b["device_id"] != old and hub.cloud_ready():
        try:
            await hub.fetch(did)  # een nieuw device-id: meteen sleutel en datamodel erbij
        except ModelError as e:
            raise ModelError(f"device-id bewaard, maar ophalen mislukt: {e}")
    return web.json_response({"ok": True})


async def tuya_fetch(request: web.Request) -> web.Response:
    await _tuya(request).fetch(request.match_info["did"])
    return web.json_response({"ok": True})


async def tuya_cloud(request: web.Request) -> web.Response:
    _tuya(request).set_cloud(await _body(request))
    return web.json_response({"ok": True})


async def tuya_set(request: web.Request) -> web.Response:
    b = await _body(request)
    _tuya(request).set(request.match_info["did"], str(b.get("dp")), str(b.get("value")))
    return web.json_response({"ok": True})


async def tuya_delete(request: web.Request) -> web.Response:
    ok = _tuya(request).remove(request.match_info["did"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


def _airco(request: web.Request):
    hub = request.app[CTX].airco
    if hub is None:
        raise ModelError("airco's zijn niet beschikbaar")
    return hub


async def airco_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    did = _airco(request).add(b)
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"w:{did}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "id": did, "key": f"w:{did}"})


async def airco_put(request: web.Request) -> web.Response:
    _airco(request).update(request.match_info["did"], await _body(request))
    return web.json_response({"ok": True})


async def airco_set(request: web.Request) -> web.Response:
    b = await _body(request)
    _airco(request).set(request.match_info["did"], str(b.get("field")), str(b.get("value")))
    return web.json_response({"ok": True})


async def airco_scan(request: web.Request) -> web.Response:
    b = await _body(request)
    host = str(b.get("host") or "").strip()
    if host and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
        raise ModelError("ongeldig adres")
    if not _airco(request).ask(host or "255.255.255.255"):
        raise ModelError("zoeken is niet mogelijk (zie het logboek)")
    return web.json_response({"ok": True})


async def airco_delete(request: web.Request) -> web.Response:
    ok = _airco(request).remove(request.match_info["did"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


def _solar(request: web.Request):
    hub = request.app[CTX].solar
    if hub is None:
        raise ModelError("omvormers zijn niet beschikbaar")
    return hub


async def solar_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    did = _solar(request).add(b)
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"o:{did}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "id": did, "key": f"o:{did}"})


async def solar_put(request: web.Request) -> web.Response:
    _solar(request).update(request.match_info["did"], await _body(request))
    return web.json_response({"ok": True})


async def solar_lookup(request: web.Request) -> web.Response:
    """Het serienummer van de stick opzoeken op zijn eigen statuspagina (die vraagt een wachtwoord; het wordt niet bewaard)."""
    b = await _body(request)
    serial = await _solar(request).lookup(str(b.get("host") or "").strip(), str(b.get("user") or "admin"), str(b.get("password") or "admin"))
    return web.json_response({"ok": True, "serial": serial})


async def solar_scan(request: web.Request) -> web.Response:
    b = await _body(request)
    host = str(b.get("host") or "").strip()
    if host and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
        raise ModelError("ongeldig adres")
    if not _solar(request).ask(host or "255.255.255.255"):
        raise ModelError("zoeken is niet mogelijk (zie het logboek)")
    return web.json_response({"ok": True})


async def solar_delete(request: web.Request) -> web.Response:
    ok = _solar(request).remove(request.match_info["did"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def screen_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    sid = ctx.model.add_screen(b.get("name"), b.get("plate") or "")
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"h:{sid}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "id": sid, "key": f"h:{sid}"})


async def screen_put(request: web.Request) -> web.Response:
    request.app[CTX].model.set_screen(request.match_info["sid"], **await _body(request))
    return web.json_response({"ok": True})


async def screen_delete(request: web.Request) -> web.Response:
    ok = request.app[CTX].model.remove_screen(request.match_info["sid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def sprinkler_run(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    op = (await _body(request)).get("op")
    if op == "start":
        ctx.engine.sprinklers.start(request.match_info["sid"])
    elif op == "stop":
        ctx.engine.sprinklers.stop(request.match_info["sid"])
    else:
        raise ModelError("op moet start of stop zijn")
    return web.json_response({"ok": True})


async def schema_name(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    schema.set_name(ctx.model, ctx.engine.snapshot(), str(b.get("key")), b.get("name"))
    return web.json_response({"ok": True})


async def schema_pages(request: web.Request) -> web.Response:
    schema.set_pages(request.app[CTX].model, (await _body(request)).get("pages"))
    return web.json_response({"ok": True})


async def schema_wire(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    schema.wire(ctx.model, ctx.engine, str(b.get("lamp")), b.get("dir"), b.get("ref"), bool(b.get("remove")))
    return web.json_response({"ok": True})


async def schema_lamp(request: web.Request) -> web.Response:
    """Een nieuw, leeg lamp-blok, meteen op zijn plek."""
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    ent = b.get("entity")
    if ent is not None and (not isinstance(ent, str) or not ENTITY_RE.match(ent)):  # voor de lamp bestaat: geen halve lamp bij een tikfout
        raise ModelError(f"ongeldige entiteit {ent!r} (verwacht bv. light.lamp_oprit)")
    gid = ctx.model.add_group(b.get("name"))
    if ent:
        schema.wire(ctx.model, ctx.engine, f"g:{gid}", "out", {"entity": ent})
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"g:{gid}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "key": f"g:{gid}"})


async def schema_switch_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    b = await _body(request)
    sid = schema.save_switch(ctx.model, None, b.get("name"), b.get("gangs"))
    if b.get("page"):
        schema.place(ctx.model, ctx.engine.snapshot(), f"d:{sid}", b["page"], b.get("x", 0), b.get("y", 0))
    return web.json_response({"ok": True, "key": f"d:{sid}"})


async def schema_switch_put(request: web.Request) -> web.Response:
    b = await _body(request)
    schema.save_switch(request.app[CTX].model, request.match_info["sid"], b.get("name"), b.get("gangs"))
    return web.json_response({"ok": True})


async def schema_switch_delete(request: web.Request) -> web.Response:
    ok = schema.remove_switch(request.app[CTX].model, request.match_info["sid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def ha_devices(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    r = await ctx.ha.devices() if ctx.ha else {"registry": False, "devices": []}
    used = {g["entity"] for gangs in ctx.model.virtual.values() for g in gangs}
    for d in r["devices"]:
        d["imported"] = all(e["entity_id"] in used for e in d["entities"])
    return web.json_response({"connected": bool(ctx.ha and ctx.ha.connected), **r})


async def devices_overview(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    ha = await ctx.ha.inventory() if ctx.ha else None
    return web.json_response({"ha": bool(ha and ha["registry"]), "devices": inventory.build(ctx.engine, ha)})


def _virtual_args(b: dict) -> tuple[str, str, list]:
    return b.get("name") or "", b.get("room") or "", b.get("gangs")


async def virtual_post(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    vid = ctx.model.set_virtual(None, *_virtual_args(await _body(request)))
    ctx.engine.resync_all()
    return web.json_response({"id": vid})


async def virtual_put(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    (vid,) = _ids(request, "vid")
    if vid not in ctx.model.virtual:
        return web.json_response({"error": "onbekend extern apparaat"}, status=404)
    ctx.model.set_virtual(vid, *_virtual_args(await _body(request)))
    ctx.engine.resync_all()
    return web.json_response({"ok": True})


async def virtual_delete(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    (vid,) = _ids(request, "vid")
    ok = ctx.model.remove_virtual(vid)
    ctx.engine.resync_all()
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def export(request: web.Request) -> web.Response:
    return web.json_response(request.app[CTX].model.to_dict(), headers={"Content-Disposition": 'attachment; filename="huisconfig.json"'})


async def import_(request: web.Request) -> web.Response:
    ctx: Ctx = request.app[CTX]
    ctx.model.load_dict(await _body(request))
    for n in sorted(ctx.engine.nodes):
        if ctx.model.adopted(n):
            ctx.engine.sync_node(n, full=True)
    return web.json_response({"ok": True})


async def secrets_(request: web.Request) -> web.Response:
    s = request.app[CTX].settings
    return web.json_response({"key_hex": s.key_hex, "secrets_h": key_header(s.key), "generated": s.key_generated})


async def ws(request: web.Request) -> web.WebSocketResponse:
    ctx: Ctx = request.app[CTX]
    sock = web.WebSocketResponse(heartbeat=25)
    await sock.prepare(request)
    ctx.clients.add(sock)
    try:
        async for msg in sock:
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        ctx.clients.discard(sock)
    return sock


def asset_version() -> str:
    """Korte vingerafdruk van de scripts en de stijl: staat als ?v= in de pagina, zodat een browser na een update nooit
    een oud script bij een nieuwe pagina gebruikt."""
    h = hashlib.sha1()
    for f in sorted(STATIC.iterdir()):
        if f.suffix in (".js", ".css"):
            h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()[:10]


async def index(request: web.Request) -> web.Response:
    v = asset_version()
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for f in ("style.css", "groups.js", "schema.js", "app.js"):
        html = html.replace(f'static/{f}"', f'static/{f}?v={v}"')
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


def make_app(ctx: Ctx) -> web.Application:
    app = web.Application(middlewares=[guard])
    app[CTX] = ctx
    app.on_response_prepare.append(_no_cache)
    r = app.router
    r.add_get("/", index)
    r.add_get("/api/state", state)
    r.add_get("/api/ws", ws)
    r.add_post("/api/light/{node}/{relay}/cmd", light_cmd)
    r.add_put("/api/light/{node}/{relay}", light_put)
    r.add_delete("/api/light/{node}/{relay}", light_delete)
    r.add_put("/api/node/{node}", node_put)
    r.add_post("/api/node/{node}/identify", node_identify)
    r.add_post("/api/node/{node}/sync", node_sync)
    r.add_put("/api/follow/{node}/{ind}", follow_put)
    r.add_post("/api/learn/start", learn_start)
    r.add_post("/api/learn/stop", learn_stop)
    r.add_post("/api/learn/save", learn_save)
    r.add_get("/api/ha/entities", ha_entities)
    r.add_get("/api/ha/devices", ha_devices)
    r.add_get("/api/devices", devices_overview)
    r.add_put("/api/plan", plan_put)
    r.add_post("/api/schema/convert", schema_convert)
    r.add_post("/api/schema/place", schema_place)
    r.add_post("/api/schema/name", schema_name)
    r.add_post("/api/schema/disable", schema_disable)
    r.add_post("/api/schema/control", schema_control)
    r.add_post("/api/timers", timer_post)
    r.add_put("/api/timers/{tid}", timer_put)
    r.add_delete("/api/timers/{tid}", timer_delete)
    r.add_post("/api/modules", module_post)
    r.add_put("/api/modules/{mid}", module_put)
    r.add_delete("/api/modules/{mid}", module_delete)
    r.add_post("/api/sprinklers", sprinkler_post)
    r.add_put("/api/sprinklers/{sid}", sprinkler_put)
    r.add_delete("/api/sprinklers/{sid}", sprinkler_delete)
    r.add_post("/api/sprinklers/{sid}/run", sprinkler_run)
    r.add_post("/api/actions", action_post)
    r.add_put("/api/actions/{aid}", action_put)
    r.add_delete("/api/actions/{aid}", action_delete)
    r.add_get("/api/panels/script", panel_script)
    r.add_post("/api/panels/{pid}/install", panel_install)
    r.add_post("/api/panels", panel_post)
    r.add_put("/api/panels/{pid}", panel_put)
    r.add_delete("/api/panels/{pid}", panel_delete)
    r.add_post("/api/panels/{pid}/relay/{n}", panel_relay)
    r.add_post("/api/panels/{pid}/yaml", panel_yaml)
    r.add_post("/api/cards", card_post)
    r.add_put("/api/link", link_put)
    r.add_post("/api/link/peers", link_peer_add)
    r.add_post("/api/link/peers/remove", link_peer_remove)
    r.add_post("/api/link/forget", link_forget)
    r.add_post("/api/link/use", link_use)
    r.add_post("/api/link/cmd", link_cmd)
    r.add_put("/api/cards/{cid}", card_put)
    r.add_delete("/api/cards/{cid}", card_delete)
    r.add_post("/api/screens", screen_post)
    r.add_put("/api/screens/{sid}", screen_put)
    r.add_delete("/api/screens/{sid}", screen_delete)
    r.add_post("/api/tuya", tuya_post)
    r.add_put("/api/tuya-cloud", tuya_cloud)
    r.add_post("/api/tuya/{did}/fetch", tuya_fetch)
    r.add_put("/api/tuya/{did}", tuya_put)
    r.add_delete("/api/tuya/{did}", tuya_delete)
    r.add_post("/api/tuya/{did}/set", tuya_set)
    r.add_post("/api/solar", solar_post)
    r.add_post("/api/solar/lookup", solar_lookup)
    r.add_post("/api/solar/scan", solar_scan)
    r.add_put("/api/solar/{did}", solar_put)
    r.add_delete("/api/solar/{did}", solar_delete)
    r.add_post("/api/airco", airco_post)
    r.add_post("/api/airco/scan", airco_scan)
    r.add_put("/api/airco/{did}", airco_put)
    r.add_delete("/api/airco/{did}", airco_delete)
    r.add_post("/api/airco/{did}/set", airco_set)
    r.add_put("/api/schema/pages", schema_pages)
    r.add_post("/api/schema/wire", schema_wire)
    r.add_post("/api/schema/lamp", schema_lamp)
    r.add_post("/api/schema/switch", schema_switch_post)
    r.add_put("/api/schema/switch/{sid}", schema_switch_put)
    r.add_delete("/api/schema/switch/{sid}", schema_switch_delete)
    r.add_post("/api/groups/parse", groups_parse)
    r.add_post("/api/groups/all-off", groups_all_off)
    r.add_post("/api/groups", groups_post)
    r.add_put("/api/groups/{gid}", group_put)
    r.add_delete("/api/groups/{gid}", group_delete)
    r.add_post("/api/groups/{gid}/cmd", group_cmd)
    r.add_post("/api/virtual", virtual_post)
    r.add_put("/api/virtual/{vid}", virtual_put)
    r.add_delete("/api/virtual/{vid}", virtual_delete)
    r.add_get("/api/export", export)
    r.add_post("/api/import", import_)
    r.add_get("/api/secrets", secrets_)
    r.add_static("/static/", STATIC, show_index=False)
    return app
